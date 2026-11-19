"""Revocation and freshness timing over 20 trials each, with warm and asleep API states
(plan 3.13, 3.18, 3.23). The loop is the real one: a signed webhook posted to the API's receiver,
the sync runner thread draining the inbox against an in-memory GitHub, and probes on every path.

What this measures is Cairn's own pipeline. GitHub's delivery latency and Render's real cold start
(about a minute) are not in it: "asleep" here means the runner is stopped at the change and starts
after a simulated wake delay. The deployment re-measure is plan 6.6.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

import psycopg
import pytest
from fastapi.testclient import TestClient

from cairn_connectors.github.fake import Delivery, FakeGitHub
from cairn_connectors.github.sync import sync_permissions
from cairn_core.testing import DbTestConfig, FreshDb, temporary_database
from cairn_eval.sync_timing import (
    Trial,
    measure_freshness,
    measure_revocation,
    summarize,
    write_parquet,
)
from cairn_worker.github_sync import GitHubSyncRunner

from .conftest import make_app_client

pytestmark = pytest.mark.integration
ORG = "kestrel-pay-demo"
SECRET = "kestrel-test-webhook-secret"
TRIALS = 20
WAKE_DELAY_S = 0.5
EVIDENCE = Path(__file__).resolve().parents[4] / "docs" / "evidence" / "phase-3" / "timing"


@pytest.fixture(scope="module")
def world(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> Iterator[tuple[FreshDb, FakeGitHub]]:
    if admin_config.settings.ingest_db_password is None:
        pytest.skip("CAIRN_INGEST_DB_PASSWORD is not set (make env-update)")
    with temporary_database(admin_conn, admin_config) as db:
        gh = FakeGitHub(org=ORG, secret=SECRET)
        gh.add_member("asha", ["payments"])
        gh.add_member("ben", ["mobile"])
        gh.add_repo("payment-service")
        gh.grant_team("payment-service", "payments")
        gh.add_repo("security-policies")
        gh.grant_team("security-policies", "security")
        yield db, gh


def _runner(db: FreshDb, gh: FakeGitHub) -> GitHubSyncRunner:
    assert db.ingest_dsn is not None
    dsn = db.ingest_dsn
    return GitHubSyncRunner(
        lambda: psycopg.connect(dsn, prepare_threshold=None), gh, ORG, poll_s=0.05, reconcile_s=1e9
    )


def test_revocation_and_freshness_p95_within_60_seconds_over_20_trials(
    world: tuple[FreshDb, FakeGitHub],
) -> None:
    db, gh = world
    assert db.ingest_dsn is not None
    # make_app_client is a generator helper; drive it by hand so the app outlives the setup
    gen = make_app_client(
        db, CAIRN_GITHUB_WEBHOOK_SECRET=SECRET, CAIRN_SYNC_WAIT_S=8, CAIRN_ANSWER_CACHE_SIZE=0
    )
    client: TestClient = next(gen)
    try:
        cookies: dict[str, str] = {}
        with psycopg.connect(db.ingest_dsn, prepare_threshold=None) as conn:
            sync_permissions(conn, gh, ORG)
            conn.commit()
        for persona in ("asha", "ben"):
            r = client.post("/auth/dev-login", json={"persona": persona})
            assert r.status_code == 204, r.text
            cookies[persona] = f"cairn_session={r.cookies.get('cairn_session')}"
        client.cookies.clear()

        def deliver(d: Delivery) -> None:
            r = client.post("/webhooks/github", content=d.body, headers=d.headers)
            assert r.status_code == 202, r.text

        def probes(persona: str, needle: str) -> dict[str, Callable[[], str]]:
            h = {"cookie": cookies[persona]}
            return {
                "search": lambda: client.post("/search", json={"query": needle}, headers=h).text,
                "answer": lambda: client.post("/answer", json={"question": needle}, headers=h).text,
            }

        runner = _runner(db, gh)
        runner.start()

        def drained() -> None:
            for _ in range(400):
                with db.connect_owner() as c:
                    row = c.execute(
                        "SELECT count(*) FROM cairn.inbox WHERE applied_at IS NULL"
                    ).fetchone()
                if row is not None and row[0] == 0:
                    return
                time.sleep(0.05)
            raise AssertionError("the inbox did not drain")

        token = "KP-ROTATION-7731"
        deliver(
            gh.put_file(
                "security-policies",
                "token-rotation.md",
                f"# Token rotation\n\nRotate keys. {token}",
            )
        )
        drained()

        trials: list[Trial] = []
        for n in range(TRIALS):
            state: Literal["warm", "asleep"] = "asleep" if n % 2 else "warm"
            gh.add_team_member("security", "ben")
            deliver(gh.add_team_member("security", "ben"))
            drained()
            assert token in "".join(p() for p in probes("ben", token).values()), (
                "positive control failed"
            )

            holder: dict[str, GitHubSyncRunner] = {"r": runner}

            def revoke(state: str = state, holder: dict[str, GitHubSyncRunner] = holder) -> None:
                if state == "asleep":
                    holder["r"].stop()  # the service is asleep when the change happens

                    def wake() -> None:
                        time.sleep(WAKE_DELAY_S)
                        fresh = _runner(db, gh)
                        holder["r"] = fresh
                        fresh.start()

                    threading.Thread(target=wake, daemon=True).start()
                deliver(gh.remove_team_member("security", "ben"))

            trials.append(
                measure_revocation(revoke, probes("ben", token), token, trial=n, api_state=state)
            )
            runner = holder["r"]
            time.sleep(0.1)
            assert runner.is_alive()

        for n in range(TRIALS):
            fstate: Literal["warm", "asleep"] = "asleep" if n % 2 else "warm"
            needle = f"KP-FRESH-{n:03d}"
            holder = {"r": runner}

            def edit(
                state: str = fstate,
                needle: str = needle,
                holder: dict[str, GitHubSyncRunner] = holder,
            ) -> None:
                if state == "asleep":
                    holder["r"].stop()

                    def wake() -> None:
                        time.sleep(WAKE_DELAY_S)
                        fresh = _runner(db, gh)
                        holder["r"] = fresh
                        fresh.start()

                    threading.Thread(target=wake, daemon=True).start()
                deliver(gh.put_pull("payment-service", 1, "Timing PR", f"Body with {needle}"))

            trials.append(
                measure_freshness(edit, probes("asha", needle), needle, trial=n, api_state=fstate)
            )
            runner = holder["r"]
            time.sleep(0.1)
        runner.stop()
    finally:
        next(gen, None)

    summary = summarize(trials)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    write_parquet(EVIDENCE / "sync-timing-2026-11-04.parquet", trials)
    (EVIDENCE / "sync-timing-2026-11-04.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert all(t.settled for t in trials), [t for t in trials if not t.settled]
    for kind in ("revocation", "freshness"):
        assert summary[f"{kind}.all"]["n"] == TRIALS
        assert summary[f"{kind}.all"]["p95_s"] <= 60, summary
        assert summary[f"{kind}.warm"]["n"] + summary[f"{kind}.asleep"]["n"] == TRIALS
