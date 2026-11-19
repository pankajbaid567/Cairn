"""Cairn worker: DBOS with a no-op ``cairn-heartbeat`` schedule (design §8, PD-07).

The worker connects as ``cairn_dbos`` to the database ``cairn_dbos`` and nowhere else. DBOS
keeps its tables in schema ``dbos`` of that database. Startup order:

1. ``load_settings`` (exit 2 on bad configuration), logging, tracing;
2. ``DBOS(config)`` and ``DBOS.launch()``, which runs DBOS's own migrations;
3. ``DBOS.apply_schedules`` for ``cairn-heartbeat``;
4. the liveness thread, which touches the heartbeat file only after steps 2 and 3 succeeded;
5. wait for SIGTERM or SIGINT, then stop the liveness thread and ``DBOS.destroy()``.

The password reaches DBOS only inside ``system_database_url``. DBOS logs that URL with the
password hidden. Nothing in this module logs the URL, the config dict or the password.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlencode

import psycopg
from dbos import DBOS, DBOSConfig, ScheduleInput
from opentelemetry import trace

from cairn_core.bootstrap import DBOS_ROLE
from cairn_core.config import WorkerSettings, libpq_tls, load_settings
from cairn_core.telemetry import configure_logging, configure_tracing
from cairn_worker.drainer_setup import make_drainer
from cairn_worker.healthcheck import ALIVE_FILE
from cairn_worker.outbox import OutboxDrainer

SERVICE_NAME: Final = "cairn-worker"
SCHEDULE_NAME: Final = "cairn-heartbeat"
SCHEDULE_CONTEXT: Final = "phase-0"
LIVENESS_INTERVAL_S: Final = 10.0
SIGNAL_POLL_S: Final = 0.25
SYS_DB_POOL_SIZE: Final = 5  # DBOS documentation: do not go below 5
DBOS_APP_NAME: Final = "cairn-worker"  # DBOS app names: [a-z0-9_-], 3 to 256 characters

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


# --- DBOS configuration ----------------------------------------------------------------------


def dbos_url(settings: WorkerSettings) -> str:
    """Connection URL for the DBOS system database. Contains the password: never log it.

    The password is percent-encoded. The scheme is plain ``postgresql``; DBOS switches the
    SQLAlchemy driver to psycopg 3 itself. The query carries ``sslmode`` and, when set,
    ``sslrootcert`` (``libpq_tls``); SQLAlchemy hands both to libpq (Requirement 7.2).
    """
    host = f"[{settings.db_host}]" if ":" in settings.db_host else settings.db_host
    password = quote(settings.dbos_db_password.get_secret_value(), safe="")
    query = urlencode(libpq_tls(settings), quote_via=quote)
    return (
        f"postgresql://{DBOS_ROLE}:{password}@{host}:{settings.db_port}/"
        f"{settings.dbos_db_name}?{query}"
    )


def describe_target(settings: WorkerSettings) -> str:
    """The same target without credentials, safe for logs."""
    return f"{DBOS_ROLE}@{settings.db_host}:{settings.db_port}/{settings.dbos_db_name}"


def dbos_config(settings: WorkerSettings) -> DBOSConfig:
    """DBOS configuration for the worker (design §8). The result holds the password."""
    return {
        "name": DBOS_APP_NAME,
        "system_database_url": dbos_url(settings),
        "sys_db_pool_size": SYS_DB_POOL_SIZE,
        "run_migrations": True,  # cairn_dbos owns its database, so it may create schema dbos
        "enable_otlp": False,  # Cairn's own tracer provider covers spans
        "log_level": settings.log_level,
    }


def prepare_dbos_logging() -> None:
    """Make DBOS log through the root logger, so ``configure_logging`` formats it as JSON.

    ``DBOS()`` calls ``init_logger``, which gives the ``dbos`` logger a plain-text console
    handler and sets ``propagate = False`` unless the logger already has a handler. A
    ``NullHandler`` added beforehand satisfies that check and writes nothing, so records
    propagate to the root handler (Verify V2).
    """
    dbos_logger = logging.getLogger("dbos")
    if not dbos_logger.handlers:
        dbos_logger.addHandler(logging.NullHandler())
    dbos_logger.propagate = True


# --- heartbeat workflow ----------------------------------------------------------------------


def heartbeat_record(scheduled_time: datetime, context: Any) -> dict[str, str]:
    """Pure description of one heartbeat run: what is logged and put on the span.

    ``scheduled_time`` is what DBOS passes (timezone-aware UTC); a naive value is read as UTC.
    Keys avoid the names of ``logging.LogRecord`` attributes, so the dict can be ``extra=``.
    """
    when = scheduled_time if scheduled_time.tzinfo else scheduled_time.replace(tzinfo=UTC)
    return {
        "schedule_name": SCHEDULE_NAME,
        "scheduled_time": when.astimezone(UTC).isoformat(),
        "schedule_context": str(context),
    }


@DBOS.workflow()
def heartbeat(scheduled_time: datetime, context: Any) -> None:
    """No-op scheduled workflow: one span and one log line per tick (Requirement 9.7).

    Returns ``None`` because DBOS types scheduled workflows as ``(datetime, Any) -> None``.
    """
    record = heartbeat_record(scheduled_time, context)
    with tracer.start_as_current_span("worker.heartbeat") as span:
        span.set_attribute("cairn.schedule_name", record["schedule_name"])
        span.set_attribute("cairn.scheduled_time", record["scheduled_time"])
        log.info("worker.heartbeat", extra=record)


def schedule_spec(
    settings: WorkerSettings,
    workflow_fn: Callable[[datetime, Any], None] = heartbeat,
) -> ScheduleInput:
    """The ``cairn-heartbeat`` entry for ``DBOS.apply_schedules``.

    ``schedule`` is the cron string from settings. DBOS reads five fields as
    ``minute hour day month weekday`` and six as ``second minute hour day month weekday``.
    ``automatic_backfill`` is off explicitly (DBOS 3.3.0 defaults to off too, FV5).
    """
    return {
        "schedule_name": SCHEDULE_NAME,
        "workflow_fn": workflow_fn,
        "schedule": settings.heartbeat_cron,
        "context": SCHEDULE_CONTEXT,
        # Missed ticks are never replayed: while Render has the service asleep nothing runs, and
        # a burst of heartbeats on wake-up would only waste Neon compute (FT-12).
        "automatic_backfill": False,
    }


# --- liveness --------------------------------------------------------------------------------


def make_db_probe(settings: WorkerSettings) -> Callable[[], None]:
    """Probe that opens a short-lived connection as ``cairn_dbos`` and runs ``SELECT 1``.

    Connection parameters go to psycopg as keywords, so no URL with a password is built.
    """

    def probe() -> None:
        with psycopg.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.dbos_db_name,
            user=DBOS_ROLE,
            password=settings.dbos_db_password.get_secret_value(),
            connect_timeout=max(1, round(settings.db_timeout_s)),
            application_name="cairn-worker-liveness",
            **libpq_tls(settings),
        ) as conn:
            conn.execute("SELECT 1")

    return probe


class LivenessThread(threading.Thread):
    """Touch ``path`` every ``every_s`` seconds, but only while ``probe`` succeeds.

    A failing probe or an unwritable path is logged (exception class and SQLSTATE only) and the
    file is left alone, so it goes stale and the healthcheck fails (Requirements 8.5, 8.6).
    """

    def __init__(
        self,
        probe: Callable[[], None],
        path: Path = ALIVE_FILE,
        every_s: float = LIVENESS_INTERVAL_S,
    ) -> None:
        super().__init__(name="cairn-worker-liveness", daemon=True)
        self._probe = probe
        self._path = path
        self._every_s = every_s
        self._stop_event = threading.Event()

    def tick(self) -> bool:
        """One probe-and-touch step. Returns True when the file was refreshed."""
        try:
            self._probe()
        except Exception as exc:  # any failure means "not alive"
            log.warning(
                "worker.liveness_probe_failed",
                extra={"error_class": type(exc).__name__, "sqlstate": _sqlstate(exc)},
            )
            return False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch()  # exist_ok: sets the mtime to now when the file exists
        except OSError as exc:
            log.warning("worker.liveness_touch_failed", extra={"error_class": type(exc).__name__})
            return False
        return True

    def run(self) -> None:
        while True:
            self.tick()
            if self._stop_event.wait(self._every_s):
                return

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)


def _sqlstate(exc: BaseException) -> str | None:
    value = getattr(exc, "sqlstate", None)
    return value if isinstance(value, str) else None


# --- signals and main ------------------------------------------------------------------------


def wait_for_signal(signals: Iterable[signal.Signals | int]) -> int:
    """Block the main thread until one of ``signals`` arrives; return its number.

    Previous handlers are restored on return. Must run in the main thread.
    """
    received: list[int] = []
    done = threading.Event()

    def handler(signum: int, _frame: object) -> None:
        received.append(signum)
        done.set()

    previous = {int(s): signal.signal(s, handler) for s in signals}
    try:
        # Not a bare done.wait(): the kernel may deliver the signal to any thread (DBOS runs
        # several), and a main thread blocked in a lock acquire is not woken by that. Waking
        # up a few times a second lets the interpreter run the Python-level handler.
        while not done.wait(SIGNAL_POLL_S):
            pass
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old)
    return received[0]


def run(settings: WorkerSettings, stop_signals: Iterable[signal.Signals | int]) -> int:
    """Launch DBOS, apply the schedule, wait for a stop signal, shut down. Returns the status."""
    prepare_dbos_logging()
    liveness: LivenessThread | None = None
    drainer: OutboxDrainer | None = None
    constructed = False
    try:
        DBOS(config=dbos_config(settings))
        constructed = True
        DBOS.launch()
        DBOS.apply_schedules([schedule_spec(settings)])
        log.info(
            "worker.started",
            extra={"target": describe_target(settings), "cron": settings.heartbeat_cron},
        )
        liveness = LivenessThread(make_db_probe(settings))
        liveness.start()
        drainer = make_drainer()  # None unless CAIRN_INGEST_DB_PASSWORD is configured (Phase 1)
        if drainer is not None:
            drainer.start()
            log.info("worker.drainer_started")
        signum = wait_for_signal(stop_signals)
        log.info("worker.stopping", extra={"signal": signal.Signals(signum).name})
        return 0
    except Exception as exc:  # report and exit non-zero so compose shows it
        log.error(
            "worker.failed",
            extra={"error_class": type(exc).__name__, "sqlstate": _sqlstate(exc)},
            exc_info=True,
        )
        return 1
    finally:
        started = time.monotonic()
        if drainer is not None:
            drainer.stop()
        if liveness is not None:
            liveness.stop()
        if constructed:
            DBOS.destroy()
        log.info("worker.stopped", extra={"shutdown_s": round(time.monotonic() - started, 2)})


def main() -> int:
    settings = load_settings(WorkerSettings)
    configure_logging(SERVICE_NAME, settings.log_level)
    configure_tracing(
        SERVICE_NAME,
        settings.otel_traces_exporter,
        str(settings.otel_endpoint) if settings.otel_endpoint else None,
        environment=settings.env,
    )
    return run(settings, (signal.SIGTERM, signal.SIGINT))
