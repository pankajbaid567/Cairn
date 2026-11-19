"""Readiness evaluation and the database probe behind ``GET /readyz`` (design §7).

Two halves:

- ``evaluate_readiness`` is pure. It turns a ``DbProbe`` (what the database said) or a
  ``DbUnavailable`` (why it said nothing) plus the expected schema head into an HTTP
  status and a ``ReadyReport``. Tests and property tests drive it directly.
- ``probe_database`` is the only code that touches Postgres. It opens one short-lived
  connection as ``cairn_app`` per call, so nothing needs a reconnect after an outage
  (Requirement 7.10), and it never lets exception text leave the function: failures
  become a fixed ``DbUnavailable`` code, and the details go to the log as exception
  class and SQLSTATE only (Requirement 7.8).

Error codes (``CheckResult.error``): ``db_unreachable``, ``db_timeout``, ``probe_failed``,
``postgres_major_mismatch``, ``extensions_missing``, ``extensions_outdated``, ``role_unsafe``,
``schema_behind``, ``schema_ahead`` and, in combined mode only, ``jobs_not_launched``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

import psycopg
from pydantic import BaseModel, Field

from cairn_core.bootstrap import MIN_EXTENSION_VERSIONS, REQUIRED_EXTENSIONS
from cairn_core.config import ApiSettings, libpq_tls

log = logging.getLogger(__name__)

EXPECTED_POSTGRES_MAJOR: Final = 18

UnavailableCode = Literal["db_unreachable", "db_timeout", "probe_failed"]

DetailValue = str | int | float | bool | None

SCHEMA_TABLE: Final = "cairn_meta.schema_migrations"

# Statement 1: server version, required extensions, the connected role's flags, and whether
# the bookkeeping table is visible. ``to_regclass`` returns NULL instead of raising when the
# table or schema does not exist, so a database that was never migrated reads as version 0.
PROBE_SQL: Final = """
SELECT current_setting('server_version_num')::int,
       coalesce((SELECT jsonb_object_agg(extname, extversion)
                   FROM pg_extension WHERE extname = ANY(%s)), '{}'::jsonb),
       r.rolname, r.rolsuper, r.rolbypassrls,
       to_regclass('cairn_meta.schema_migrations') IS NOT NULL
  FROM pg_roles AS r
 WHERE r.rolname = current_user
"""

# Statement 2, only when the table exists.
SCHEMA_VERSION_SQL: Final = "SELECT max(version) FROM cairn_meta.schema_migrations"


@dataclass(frozen=True)
class DbProbe:
    """What one successful probe saw."""

    server_version_num: int  # e.g. 180001
    extensions: Mapping[str, str]  # extname -> extversion, required extensions only
    role_name: str
    role_is_superuser: bool
    role_bypasses_rls: bool
    schema_version: int | None  # max(version) in cairn_meta.schema_migrations; None: no table/rows
    latency_ms: float


@dataclass(frozen=True)
class JobsStatus:
    """Combined mode only: whether DBOS has launched, and how many launch attempts were made.
    A snapshot taken by the jobs runner; it holds nothing that could carry a secret."""

    launched: bool
    attempts: int
    sync_ready: bool | None = (
        None  # live sync configured: has its first pass completed? (plan 3.28)
    )


JobsProbe = Callable[[], JobsStatus]
"""An injectable jobs status source: ``/readyz`` calls it once per request in combined mode."""


@dataclass(frozen=True)
class DbUnavailable:
    """The probe could not produce a ``DbProbe``. Carries a fixed code and nothing else, so no
    exception text, host name or credential can reach a response body."""

    code: UnavailableCode


ProbeResult = DbProbe | DbUnavailable
ProbeFn = Callable[[], Awaitable[ProbeResult]]
"""An injectable probe: the API calls it once per ``/readyz`` request."""


class CheckResult(BaseModel):
    ok: bool
    error: str | None = None  # stable code, never exception text
    detail: dict[str, DetailValue] = Field(default_factory=dict)

    def to_body(self) -> dict[str, Any]:
        """JSON-ready form: ``error`` and ``detail`` appear only when set."""
        body: dict[str, Any] = {"ok": self.ok}
        if self.error is not None:
            body["error"] = self.error
        if self.detail:
            body["detail"] = dict(self.detail)
        return body


class ReadyReport(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckResult]  # database, postgres, extensions, role, schema (+ jobs)
    extensions: dict[str, str] = Field(default_factory=dict)  # present required extensions

    def to_body(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": {name: check.to_body() for name, check in self.checks.items()},
            "extensions": dict(self.extensions),
        }


def _check(ok: bool, error: str, **detail: DetailValue) -> CheckResult:
    return CheckResult(ok=ok, error=None if ok else error, detail=dict(detail))


def postgres_version_text(server_version_num: int) -> str:
    """``180001`` -> ``18.1`` (PostgreSQL 10 and later: major * 10000 + minor)."""
    return f"{server_version_num // 10000}.{server_version_num % 10000}"


_EXTENSION_VERSION_RE: Final = re.compile(r"[0-9]+(\.[0-9]+)*")


def parse_extension_version(text: str) -> tuple[int, ...] | None:
    """``"0.8.1"`` -> ``(0, 8, 1)``; ``None`` for anything that is not dotted digits."""
    if not _EXTENSION_VERSION_RE.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def meets_minimum(version: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    """Compare dotted versions with missing parts read as 0 (``(0, 8)`` meets ``(0, 8, 0)``)."""
    width = max(len(version), len(minimum))
    return version + (0,) * (width - len(version)) >= minimum + (0,) * (width - len(minimum))


def outdated_extensions(extensions: Mapping[str, str]) -> list[str]:
    """Present extensions below their ``MIN_EXTENSION_VERSIONS`` floor, in required order.

    A version that cannot be parsed counts as outdated: it cannot be shown to meet the floor.
    """
    outdated: list[str] = []
    for name in REQUIRED_EXTENSIONS:
        minimum = MIN_EXTENSION_VERSIONS.get(name)
        if minimum is None or name not in extensions:
            continue
        parsed = parse_extension_version(extensions[name])
        if parsed is None or not meets_minimum(parsed, minimum):
            outdated.append(name)
    return outdated


def _extensions_check(extensions: Mapping[str, str]) -> CheckResult:
    """``extensions_missing`` when a required extension is absent, else ``extensions_outdated``
    when one is below its floor. The detail names the extensions, never more."""
    missing = [name for name in REQUIRED_EXTENSIONS if name not in extensions]
    outdated = outdated_extensions(extensions)
    detail: dict[str, DetailValue] = {}
    if missing:
        detail["missing"] = ",".join(missing)
    if outdated:
        detail["outdated"] = ",".join(outdated)
    error = "extensions_missing" if missing else "extensions_outdated"
    return _check(not (missing or outdated), error, **detail)


def _jobs_check(jobs: JobsStatus) -> CheckResult:
    return _check(jobs.launched, "jobs_not_launched", attempts=jobs.attempts)


def evaluate_readiness(
    probe: ProbeResult, expected_head: int, jobs: JobsStatus | None = None
) -> tuple[int, ReadyReport]:
    """Pure. 200 if and only if every check passes; otherwise 503 with the failing codes.
    ``expected_head`` is the highest bundled migration version (at least 1). Extensions:
    a missing one gives ``extensions_missing``; otherwise one below ``MIN_EXTENSION_VERSIONS``
    gives ``extensions_outdated`` (retarget FT-03). ``jobs`` is given only in combined mode
    (FT-11): a ``jobs`` check is then part of the report, whether or not the database answered,
    and fails with ``jobs_not_launched`` until DBOS has launched.
    """
    if isinstance(probe, DbUnavailable):
        checks = {"database": CheckResult(ok=False, error=probe.code)}
        if jobs is not None:
            checks["jobs"] = _jobs_check(jobs)
            if jobs.sync_ready is not None:
                checks["sync"] = _check(jobs.sync_ready, "sync_not_ready")
        return 503, ReadyReport(status="not_ready", checks=checks, extensions={})
    version = probe.schema_version or 0
    schema_error = "schema_behind" if version < expected_head else "schema_ahead"
    checks = {
        "database": CheckResult(ok=True, detail={"latency_ms": round(probe.latency_ms, 1)}),
        "postgres": _check(
            probe.server_version_num // 10000 == EXPECTED_POSTGRES_MAJOR,
            "postgres_major_mismatch",
            version=postgres_version_text(probe.server_version_num),
        ),
        "extensions": _extensions_check(probe.extensions),
        "role": _check(
            not (probe.role_is_superuser or probe.role_bypasses_rls),
            "role_unsafe",
            name=probe.role_name,
        ),
        "schema": _check(
            version == expected_head, schema_error, version=version, expected=expected_head
        ),
    }
    if jobs is not None:
        checks["jobs"] = _jobs_check(jobs)
        if jobs.sync_ready is not None:
            # Drain-before-serve: not ready until the start-up permission poll has been applied.
            checks["sync"] = _check(jobs.sync_ready, "sync_not_ready")
    ready = all(check.ok for check in checks.values())
    present = {
        name: probe.extensions[name] for name in REQUIRED_EXTENSIONS if name in probe.extensions
    }
    report = ReadyReport(
        status="ready" if ready else "not_ready", checks=checks, extensions=present
    )
    return (200 if ready else 503), report


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def _failure(exc: BaseException) -> DbUnavailable:
    """Map an exception to a fixed code. Only the exception class and SQLSTATE are logged."""
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(exc, TimeoutError | psycopg.errors.QueryCanceled):
        code: UnavailableCode = "db_timeout"
    elif isinstance(exc, psycopg.OperationalError | OSError):
        # libpq reports its own connect timeout as an OperationalError with no SQLSTATE; the
        # text is only inspected here, never stored or returned.
        timed_out = sqlstate is None and "timeout" in str(exc).lower()
        code = "db_timeout" if timed_out else "db_unreachable"
    else:
        code = "probe_failed"
    log.warning(
        "readyz.probe_failed",
        extra={"error_class": type(exc).__name__, "sqlstate": sqlstate, "reason": code},
    )
    return DbUnavailable(code)


async def _run_probe(settings: ApiSettings, started: float) -> DbProbe:
    timeout_ms = max(1, round(settings.db_timeout_s * 1000))
    conn = await psycopg.AsyncConnection.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.app_db_user,
        password=settings.app_db_password.get_secret_value(),
        connect_timeout=max(1, math.ceil(settings.db_timeout_s)),
        application_name="cairn-api-readyz",
        options=f"-c statement_timeout={timeout_ms} -c default_transaction_read_only=on",
        autocommit=True,
        **libpq_tls(settings),
    )
    async with conn, conn.transaction():
        cur = await conn.execute(PROBE_SQL, [list(REQUIRED_EXTENSIONS)])
        row = await cur.fetchone()
        if row is None:  # current_user always has a pg_roles row; defensive only
            raise psycopg.DataError("role row missing")
        version_num, extensions, role_name, is_super, bypass_rls, has_table = row
        schema_version: int | None = None
        if has_table:
            cur = await conn.execute(SCHEMA_VERSION_SQL)
            version_row = await cur.fetchone()
            schema_version = version_row[0] if version_row else None
    return DbProbe(
        server_version_num=int(version_num),
        extensions={str(k): str(v) for k, v in dict(extensions).items()},
        role_name=str(role_name),
        role_is_superuser=bool(is_super),
        role_bypasses_rls=bool(bypass_rls),
        schema_version=None if schema_version is None else int(schema_version),
        latency_ms=(time.perf_counter() - started) * 1000,
    )


async def probe_database(settings: ApiSettings) -> ProbeResult:
    """Open one short-lived connection as ``cairn_app``, run at most two read-only statements,
    close it. Bounded end to end by ``settings.db_timeout_s``. Never raises (except for task
    cancellation): every failure is returned as a ``DbUnavailable`` code."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(settings.db_timeout_s):
            return await _run_probe(settings, started)
    except Exception as exc:
        return _failure(exc)
