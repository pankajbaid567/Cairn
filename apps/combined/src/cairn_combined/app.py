"""The combined process: the API and the DBOS jobs in one service (free-tier design §7).

Render's free plan has no background workers, so one web service runs both. uvicorn serves the
FastAPI app in the main thread. A ``JobsRunner`` thread, started by the app's lifespan, launches
DBOS and applies the ``cairn-heartbeat`` schedule. Launching in the background keeps ``/healthz``
answering 200 while the database is asleep or down (Requirement 8.2); ``/readyz`` shows the state
through its ``jobs`` check until DBOS is up (Requirement 8.6).

A failed launch is logged (exception class and SQLSTATE, never the message), DBOS is destroyed,
and the runner waits ``min(cap, 2 ** (attempt - 1))`` seconds before the next attempt. DBOS 3.3.0
can be constructed again in the same process after ``DBOS.destroy()`` (FV5), so there is no
give-up path: Requirement 8.4 applies only when it cannot, and the runner retries until it works
or is stopped.

The password reaches DBOS only inside ``system_database_url`` (``cairn_worker.app.dbos_url``).
Nothing here logs the URL, the config dict or a password.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

import uvicorn
from dbos import DBOS
from fastapi import FastAPI
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from starlette.types import Lifespan

from cairn_api.main import build_app, maybe_search_runtime
from cairn_core.config import CombinedSettings, load_settings
from cairn_core.health import JobsStatus
from cairn_core.telemetry import configure_logging, configure_tracing
from cairn_worker.app import dbos_config, prepare_dbos_logging, schedule_spec
from cairn_worker.drainer_setup import make_drainer
from cairn_worker.outbox import OutboxDrainer

if TYPE_CHECKING:
    from cairn_worker.github_sync import GitHubSyncRunner

SERVICE_NAME: Final = "cairn-combined"
HOST: Final = "0.0.0.0"  # the container binds all interfaces; Render routes to it  # noqa: S104
GRACEFUL_SHUTDOWN_S: Final = 20
"""uvicorn's budget for draining connections after SIGTERM (Requirement 8.7)."""
RUNNER_STOP_TIMEOUT_S: Final = 15.0
"""How long the lifespan waits for the runner at shutdown; leaves room inside the 20 s."""
MAX_BACKOFF_EXPONENT: Final = 32
"""``2 ** 32`` is far above the largest allowed cap (600 s); the clamp only avoids huge ints."""

log = logging.getLogger(__name__)


def _default_sync_factory() -> GitHubSyncRunner | None:
    """Imported on first use: a process without GitHub sync never loads the connectors package."""
    from cairn_worker.github_sync import make_sync_runner

    return make_sync_runner()


def backoff_schedule(attempt: int, cap_s: float) -> float:
    """Seconds to wait after failed attempt number ``attempt`` (1 for the first): 1, 2, 4 ...,
    never more than ``cap_s``."""
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    return min(cap_s, float(2 ** min(attempt - 1, MAX_BACKOFF_EXPONENT)))


def sqlstate_of(exc: BaseException) -> str | None:
    """SQLSTATE of ``exc`` or of the exception it was raised from, if any."""
    found: BaseException | None = exc
    for _ in range(5):  # a short chain; DBOS and SQLAlchemy wrap one level
        if found is None:
            break
        value = getattr(found, "sqlstate", None)
        if isinstance(value, str):
            return value
        found = found.__cause__
    return None


def launch_dbos(settings: CombinedSettings) -> None:
    """Construct DBOS, launch it (runs DBOS's own migrations) and apply the heartbeat schedule.

    Raises whatever DBOS raises; the runner logs the class and SQLSTATE and calls
    ``destroy_dbos`` before it tries again.
    """
    prepare_dbos_logging()  # DBOS() resets its logger each time, so this runs per attempt
    DBOS(config=dbos_config(settings))
    DBOS.launch()
    DBOS.apply_schedules([schedule_spec(settings)])


def destroy_dbos() -> None:
    """Tear down the current DBOS instance, launched or half built. Safe when none exists."""
    DBOS.destroy()


class JobsRunner(threading.Thread):
    """Launches DBOS and applies the heartbeat schedule, retrying until it works or ``stop()``.

    ``launch`` and ``destroy`` are injectable so tests need neither DBOS nor a database. ``wait``
    sleeps for the backoff and returns True when stop was requested (``Event.wait`` semantics).
    """

    def __init__(
        self,
        settings: CombinedSettings,
        *,
        launch: Callable[[CombinedSettings], None] = launch_dbos,
        destroy: Callable[[], None] = destroy_dbos,
        wait: Callable[[float], bool] | None = None,
        max_backoff_s: float | None = None,
        drainer: Callable[[], OutboxDrainer | None] | None = None,
        sync_runner: Callable[[], GitHubSyncRunner | None] | None = None,
    ) -> None:
        super().__init__(name="cairn-jobs-runner", daemon=True)
        self._settings = settings
        self._launch = launch
        self._destroy = destroy
        self._stop_event = threading.Event()
        self._wait = wait if wait is not None else self._stop_event.wait
        self._max_backoff_s = settings.jobs_retry_max_s if max_backoff_s is None else max_backoff_s
        self._lock = threading.Lock()
        self._launched = False
        self._attempts = 0
        self._drainer_factory = drainer if drainer is not None else make_drainer
        self._drainer: OutboxDrainer | None = None
        self._sync_factory = sync_runner if sync_runner is not None else _default_sync_factory
        self._sync: GitHubSyncRunner | None = None

    def status(self) -> JobsStatus:
        """Thread-safe snapshot for ``/readyz``."""
        with self._lock:
            return JobsStatus(
                launched=self._launched,
                attempts=self._attempts,
                sync_ready=self._sync.started_ok.is_set() if self._sync is not None else None,
            )

    def run(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                self._attempts += 1
                attempt = self._attempts
            try:
                self._launch(self._settings)
            except Exception as exc:  # any failure: report class and SQLSTATE, then retry
                delay = backoff_schedule(attempt, self._max_backoff_s)
                log.warning(
                    "jobs.launch_failed",
                    extra={
                        "attempt": attempt,
                        "error_class": type(exc).__name__,
                        "sqlstate": sqlstate_of(exc),
                        "retry_in_s": delay,
                    },
                )
                self._destroy_quietly()
                if self._wait(delay):
                    return
                continue
            with self._lock:
                self._launched = True
            log.info("jobs.launched", extra={"attempt": attempt})
            self._start_drainer()
            return

    def stop(self, timeout: float = 20.0) -> None:
        """Ask the loop to end, wait up to ``timeout`` seconds for it, and destroy DBOS if it
        had launched. Idempotent."""
        self._stop_event.set()
        if self.is_alive() and self is not threading.current_thread():
            self.join(timeout)
            if self.is_alive():
                log.warning("jobs.stop_timeout", extra={"timeout_s": timeout})
        if self._drainer is not None:  # before DBOS.destroy: it enqueues through DBOS (PD1-28)
            self._drainer.stop()
            self._drainer = None
        if self._sync is not None:
            self._sync.stop()
            self._sync = None
        with self._lock:
            launched, self._launched = self._launched, False
        if launched:
            self._destroy_quietly()
            log.info("jobs.stopped")

    def _start_sync(self) -> None:
        try:
            runner = self._sync_factory()
        except Exception as exc:  # a configuration fault must not take the API down
            log.error("jobs.sync_not_started", extra={"error_class": type(exc).__name__})
            return
        if runner is not None:
            runner.start()
            self._sync = runner
            log.info("jobs.sync_started")

    def _start_drainer(self) -> None:
        self._start_sync()
        try:
            drainer = self._drainer_factory()
        except Exception as exc:  # a configuration fault must not take the API down
            log.error("jobs.drainer_not_started", extra={"error_class": type(exc).__name__})
            return
        if drainer is not None:
            drainer.start()
            self._drainer = drainer
            log.info("jobs.drainer_started")

    def _destroy_quietly(self) -> None:
        try:
            self._destroy()
        except Exception as exc:  # a failing teardown must not stop the retry loop
            log.warning(
                "jobs.destroy_failed",
                extra={"error_class": type(exc).__name__, "sqlstate": sqlstate_of(exc)},
            )


def runner_lifespan(runner: JobsRunner) -> Lifespan[FastAPI]:
    """FastAPI lifespan: start the runner at startup, stop it (and DBOS) at shutdown."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        runner.start()
        try:
            yield
        finally:
            # stop() joins a thread and destroys DBOS, which blocks: keep it off the event loop.
            await asyncio.to_thread(runner.stop, RUNNER_STOP_TIMEOUT_S)

    return lifespan


def create_app() -> FastAPI:
    """Uvicorn factory. Exits with status 2 on invalid configuration, before any port is bound
    (Requirement 8.8)."""
    settings = load_settings(CombinedSettings)
    configure_logging(SERVICE_NAME, settings.log_level)
    provider = configure_tracing(
        SERVICE_NAME,
        settings.otel_traces_exporter,
        str(settings.otel_endpoint) if settings.otel_endpoint else None,
        environment=settings.env,
    )
    PsycopgInstrumentor().instrument(tracer_provider=provider)
    runner = JobsRunner(settings)
    return build_app(
        settings,
        tracer_provider=provider,
        jobs=runner.status,
        lifespan=runner_lifespan(runner),
        search=maybe_search_runtime(settings),
    )


def _terminate(code: int) -> None:
    """Flush logs and leave without interpreter finalization (replaced in tests)."""
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def main() -> None:
    """Serve on ``0.0.0.0:$PORT`` (default 10000). Settings are loaded first, so a bad
    configuration exits with status 2 before uvicorn binds anything."""
    settings = load_settings(CombinedSettings)
    uvicorn.run(
        create_app,
        factory=True,
        host=HOST,
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    # The lifespan has already stopped the runner, DBOS and the pools. On the 0.1 CPU free-tier
    # instance, interpreter finalization (garbage-collecting about a thousand modules) takes longer
    # than the stop grace period, so the platform would kill the process anyway. Flush and leave.
    _terminate(0)
