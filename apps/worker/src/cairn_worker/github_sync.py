"""The live sync runner: permission poll on start, inbox drain on an interval, reconciliation
(plan 3.5, 3.7, 3.10, 3.28).

One thread. On start it asks for an ACL-class poll (a synthetic inbox row, so the API's barrier is
closed until it is applied), drains, and reconciles. Afterwards it drains every ``sync_poll_s`` and
re-polls permissions and reconciles every ``reconcile_interval_s`` *while the service is awake*;
there is no always-on poller on Render free (ADR-0014). A failed pass is logged and retried; it
never stops the thread, and a failed GitHub call leaves rows pending (and queries closed) rather
than guessing.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import psycopg

from cairn_connectors.github.client import GitHubClient, GitHubError
from cairn_connectors.github.sync import DrainStats, drain, reconcile
from cairn_core.config import IngestSettings, load_settings
from cairn_core.db import connect
from cairn_worker.drainer_setup import ingest_configured

log = logging.getLogger(__name__)


class GitHubSyncRunner(threading.Thread):
    def __init__(
        self,
        connect_db: Callable[[], psycopg.Connection[Any]],
        client: GitHubClient,
        org: str,
        *,
        user_map: Mapping[str, str] | None = None,
        poll_s: float = 5.0,
        reconcile_s: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="cairn-github-sync", daemon=True)
        self._connect, self._gh, self._org = connect_db, client, org
        self._user_map = dict(user_map or {})
        self._poll_s, self._reconcile_s, self._clock = poll_s, reconcile_s, clock
        self._stop_event = threading.Event()
        self.started_ok = (
            threading.Event()
        )  # set after the first full pass: the "sync check" for readiness
        self.passes = 0
        self.last_error: str | None = None

    def request_permission_poll(self, conn: psycopg.Connection[Any], reason: str) -> None:
        """Queue an ACL-class poll row. Queries stay closed until the drainer applies it."""
        conn.execute(
            "INSERT INTO cairn.inbox (delivery_id, event, action, class, priority) "
            "VALUES (%s, 'poll', %s, 'acl', 0) ON CONFLICT DO NOTHING",
            (f"poll-{reason}-{uuid.uuid4().hex[:12]}", reason),
        )
        conn.commit()

    def pass_once(
        self, conn: psycopg.Connection[Any], *, reconcile_too: bool = False
    ) -> DrainStats:
        stats = drain(conn, self._gh, self._org, user_map=self._user_map)
        if reconcile_too:
            out = reconcile(conn, self._gh, self._org)
            if out.errors:
                log.warning("sync.reconcile_errors", extra={"count": len(out.errors)})
            if out.backfilled or out.redelivered:
                stats = drain(conn, self._gh, self._org, user_map=self._user_map)
        self.passes += 1
        return stats

    def run(self) -> None:
        last_reconcile = float("-inf")
        conn: psycopg.Connection[Any] | None = None
        first = True
        while not self._stop_event.is_set():
            try:
                if conn is None or conn.closed:
                    conn = self._connect()
                if first:
                    self.request_permission_poll(conn, "start")
                due = self._clock() - last_reconcile >= self._reconcile_s
                if due and not first:
                    self.request_permission_poll(conn, "scheduled")
                self.pass_once(conn, reconcile_too=due)
                if due:
                    last_reconcile = self._clock()
                self.last_error = None
                if first:
                    first = False
                    self.started_ok.set()
                    log.info("sync.started")
            except (GitHubError, psycopg.Error) as exc:
                self.last_error = type(exc).__name__
                log.warning("sync.pass_failed", extra={"error_class": type(exc).__name__})
                if conn is not None:
                    try:
                        conn.rollback()
                    except psycopg.Error:
                        conn = None
            if self._stop_event.wait(self._poll_s):
                break
        if conn is not None and not conn.closed:
            conn.close()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)


def make_sync_runner(
    client_factory: Callable[[IngestSettings], GitHubClient] | None = None,
) -> GitHubSyncRunner | None:
    """The runner for the configured organisation, or ``None`` when live sync is not configured."""
    if not ingest_configured():  # a Phase 0 deployment has no ingest login
        return None
    try:
        settings = load_settings(IngestSettings)
    except SystemExit:  # load_settings exits on an invalid configuration; sync is simply off
        return None
    if not settings.github_org:
        return None
    factory = client_factory or _http_client
    user_map: dict[str, str] = {}
    if settings.github_user_map:
        user_map = json.loads(Path(settings.github_user_map).read_text(encoding="utf-8"))

    def open_conn() -> psycopg.Connection[Any]:
        return connect(
            settings,
            settings.ingest_db_user,
            settings.ingest_db_password.get_secret_value(),
            application_name="cairn-github-sync",
        )

    return GitHubSyncRunner(
        open_conn,
        factory(settings),
        settings.github_org,
        user_map=user_map,
        poll_s=settings.sync_poll_s,
        reconcile_s=settings.reconcile_interval_s,
    )


def _http_client(settings: IngestSettings) -> GitHubClient:
    from cairn_connectors.github.http import HttpGitHub

    if not (
        settings.github_app_id and settings.github_installation_id and settings.github_private_key
    ):
        raise GitHubError(
            "CAIRN_GITHUB_APP_ID, CAIRN_GITHUB_INSTALLATION_ID and CAIRN_GITHUB_PRIVATE_KEY are required"  # noqa: E501
        )
    pem = settings.github_private_key.get_secret_value().replace("\\n", "\n").encode()
    return HttpGitHub(settings.github_app_id, settings.github_installation_id, pem)
