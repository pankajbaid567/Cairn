"""Unit tests for cairn_core.health: readiness codes, response bodies, probe failure mapping.

No database is needed. The probe itself is exercised only against ports that nothing serves
(connection refused) or that accept and stay silent (timeout); the live path is covered by the
integration tests of task 5.4.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import psycopg
import pytest

from cairn_core.bootstrap import MIN_EXTENSION_VERSIONS, REQUIRED_EXTENSIONS
from cairn_core.config import ApiSettings
from cairn_core.health import (
    DbProbe,
    DbUnavailable,
    JobsStatus,
    evaluate_readiness,
    meets_minimum,
    parse_extension_version,
    postgres_version_text,
    probe_database,
)

EXPECTED_HEAD = 1
EXTENSIONS = {"vector": "0.8.1", "btree_gist": "1.8"}
SECRET = "s3cr3t-pw-do-not-print"


def make_probe(**overrides: object) -> DbProbe:
    values: dict[str, object] = {
        "server_version_num": 180001,
        "extensions": dict(EXTENSIONS),
        "role_name": "cairn_app",
        "role_is_superuser": False,
        "role_bypasses_rls": False,
        "schema_version": EXPECTED_HEAD,
        "latency_ms": 3.14159,
    }
    values.update(overrides)
    return DbProbe(**values)  # type: ignore[arg-type]


def failing_codes(probe: DbProbe | DbUnavailable, head: int = EXPECTED_HEAD) -> dict[str, str]:
    _, report = evaluate_readiness(probe, head)
    return {name: c.error for name, c in report.checks.items() if not c.ok and c.error}


def settings_for(port: int, *, timeout_s: float = 2.0) -> ApiSettings:
    # model_validate bypasses the environment and .env, so the test is hermetic.
    return ApiSettings.model_validate(
        {
            "CAIRN_DB_HOST": "127.0.0.1",
            "CAIRN_DB_PORT": port,
            "CAIRN_DB_TIMEOUT_S": timeout_s,
            "CAIRN_APP_DB_PASSWORD": SECRET,
        }
    )


@contextmanager
def silent_listener() -> Iterator[int]:
    """A TCP port that completes the handshake (via the backlog) and never answers."""
    with closing(socket.socket()) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        yield int(server.getsockname()[1])


def closed_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --- evaluate_readiness ---------------------------------------------------------------


def test_all_checks_pass_gives_200_and_the_documented_body() -> None:
    status, report = evaluate_readiness(make_probe(), EXPECTED_HEAD)
    assert status == 200
    assert report.status == "ready"
    assert report.to_body() == {
        "status": "ready",
        "checks": {
            "database": {"ok": True, "detail": {"latency_ms": 3.1}},
            "postgres": {"ok": True, "detail": {"version": "18.1"}},
            "extensions": {"ok": True},
            "role": {"ok": True, "detail": {"name": "cairn_app"}},
            "schema": {"ok": True, "detail": {"version": 1, "expected": 1}},
        },
        "extensions": {"btree_gist": "1.8", "vector": "0.8.1"},
    }
    json.dumps(report.to_body())  # serializable as is


@pytest.mark.parametrize(
    ("unavailable", "code"),
    [
        ("db_unreachable", "db_unreachable"),
        ("db_timeout", "db_timeout"),
        ("probe_failed", "probe_failed"),
    ],
)
def test_unavailable_database_gives_503_with_only_the_database_check(
    unavailable: str, code: str
) -> None:
    status, report = evaluate_readiness(DbUnavailable(unavailable), EXPECTED_HEAD)  # type: ignore[arg-type]
    assert status == 503
    assert report.to_body() == {
        "status": "not_ready",
        "checks": {"database": {"ok": False, "error": code}},
        "extensions": {},
    }


@pytest.mark.parametrize("num", [170005, 160010, 190000, 0])
def test_other_postgres_major_versions_give_postgres_major_mismatch(num: int) -> None:
    status, _ = evaluate_readiness(make_probe(server_version_num=num), EXPECTED_HEAD)
    assert status == 503
    assert failing_codes(make_probe(server_version_num=num)) == {
        "postgres": "postgres_major_mismatch"
    }


def test_any_postgres_18_minor_passes() -> None:
    for num in (180000, 180001, 189999):
        assert evaluate_readiness(make_probe(server_version_num=num), EXPECTED_HEAD)[0] == 200


@pytest.mark.parametrize("missing", [("vector",), ("btree_gist",), REQUIRED_EXTENSIONS])
def test_missing_extensions_are_named_and_the_present_ones_still_listed(
    missing: tuple[str, ...],
) -> None:
    present = {k: v for k, v in EXTENSIONS.items() if k not in missing}
    probe = make_probe(extensions=present)
    status, report = evaluate_readiness(probe, EXPECTED_HEAD)
    assert status == 503
    check = report.checks["extensions"]
    assert check.error == "extensions_missing"
    assert set(check.detail["missing"].split(",")) == set(missing)  # type: ignore[union-attr]
    assert report.extensions == present
    assert failing_codes(probe) == {"extensions": "extensions_missing"}


def test_required_extensions_are_vector_and_btree_gist_with_a_vector_floor() -> None:
    assert set(REQUIRED_EXTENSIONS) == {"vector", "btree_gist"}
    assert MIN_EXTENSION_VERSIONS == {"vector": (0, 8, 0)}


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("0.8.1", (0, 8, 1)),
        ("1.8", (1, 8)),
        ("12", (12,)),
        ("0.8.1-dev", None),
        ("", None),
        ("v0.8.0", None),
        ("0..8", None),
    ],
)
def test_parse_extension_version(text: str, parsed: tuple[int, ...] | None) -> None:
    assert parse_extension_version(text) == parsed


def test_meets_minimum_pads_missing_parts_with_zero() -> None:
    assert meets_minimum((0, 8), (0, 8, 0))
    assert meets_minimum((0, 8, 0), (0, 8, 0))
    assert meets_minimum((1,), (0, 8, 0))
    assert not meets_minimum((0, 7, 9), (0, 8, 0))
    assert not meets_minimum((0, 7), (0, 8, 0))


@pytest.mark.parametrize("version", ["0.7.4", "0.5", "0", "unknown"])
def test_vector_below_the_floor_gives_extensions_outdated(version: str) -> None:
    probe = make_probe(extensions={**EXTENSIONS, "vector": version})
    status, report = evaluate_readiness(probe, EXPECTED_HEAD)
    assert status == 503
    assert failing_codes(probe) == {"extensions": "extensions_outdated"}
    assert report.checks["extensions"].detail == {"outdated": "vector"}
    assert report.extensions == {"btree_gist": "1.8", "vector": version}


@pytest.mark.parametrize("version", ["0.8.0", "0.8", "0.8.6", "1.0.0"])
def test_vector_at_or_above_the_floor_passes(version: str) -> None:
    probe = make_probe(extensions={**EXTENSIONS, "vector": version})
    assert evaluate_readiness(probe, EXPECTED_HEAD)[0] == 200


def test_a_missing_extension_wins_over_an_outdated_one() -> None:
    probe = make_probe(extensions={"vector": "0.7.0"})
    _, report = evaluate_readiness(probe, EXPECTED_HEAD)
    check = report.checks["extensions"]
    assert check.error == "extensions_missing"
    assert check.detail == {"missing": "btree_gist", "outdated": "vector"}


def test_extensions_outside_the_required_set_are_not_reported() -> None:
    probe = make_probe(extensions={**EXTENSIONS, "pg_stat_statements": "1.11"})
    _, report = evaluate_readiness(probe, EXPECTED_HEAD)
    assert set(report.extensions) == set(REQUIRED_EXTENSIONS)


@pytest.mark.parametrize(
    "flags",
    [
        {"role_is_superuser": True},
        {"role_bypasses_rls": True},
        {"role_is_superuser": True, "role_bypasses_rls": True},
    ],
)
def test_superuser_or_bypassrls_role_gives_role_unsafe(flags: dict[str, bool]) -> None:
    probe = make_probe(role_name="postgres", **flags)
    status, _ = evaluate_readiness(probe, EXPECTED_HEAD)
    assert status == 503
    assert failing_codes(probe) == {"role": "role_unsafe"}


@pytest.mark.parametrize(
    ("recorded", "head", "code"),
    [
        (None, 1, "schema_behind"),  # never migrated reads as version 0
        (1, 2, "schema_behind"),
        (3, 2, "schema_ahead"),
        (2, 1, "schema_ahead"),
    ],
)
def test_schema_version_must_equal_the_head(recorded: int | None, head: int, code: str) -> None:
    probe = make_probe(schema_version=recorded)
    status, report = evaluate_readiness(probe, head)
    assert status == 503
    assert failing_codes(probe, head) == {"schema": code}
    assert report.checks["schema"].detail == {"version": recorded or 0, "expected": head}


def test_several_failures_are_all_reported_with_their_own_codes() -> None:
    probe = make_probe(
        server_version_num=170000,
        extensions={},
        role_is_superuser=True,
        schema_version=None,
    )
    assert failing_codes(probe) == {
        "postgres": "postgres_major_mismatch",
        "extensions": "extensions_missing",
        "role": "role_unsafe",
        "schema": "schema_behind",
    }


# --- the jobs check (combined mode, FT-11) -------------------------------------------------


def test_without_a_jobs_status_the_body_has_no_jobs_check() -> None:
    _, report = evaluate_readiness(make_probe(), EXPECTED_HEAD)
    assert "jobs" not in report.checks


def test_a_launched_jobs_runner_keeps_readiness_at_200() -> None:
    status, report = evaluate_readiness(make_probe(), EXPECTED_HEAD, JobsStatus(True, 2))
    assert status == 200
    assert report.checks["jobs"].to_body() == {"ok": True, "detail": {"attempts": 2}}
    assert list(report.checks) == ["database", "postgres", "extensions", "role", "schema", "jobs"]


def test_jobs_not_launched_gives_503_even_when_the_database_checks_pass() -> None:
    status, report = evaluate_readiness(make_probe(), EXPECTED_HEAD, JobsStatus(False, 3))
    assert status == 503
    assert report.status == "not_ready"
    assert report.checks["jobs"].to_body() == {
        "ok": False,
        "error": "jobs_not_launched",
        "detail": {"attempts": 3},
    }
    assert {n for n, c in report.checks.items() if not c.ok} == {"jobs"}


def test_the_jobs_check_is_reported_when_the_database_is_unavailable_too() -> None:
    status, report = evaluate_readiness(
        DbUnavailable("db_unreachable"), EXPECTED_HEAD, JobsStatus(False, 1)
    )
    assert status == 503
    assert {n: c.error for n, c in report.checks.items()} == {
        "database": "db_unreachable",
        "jobs": "jobs_not_launched",
    }
    assert report.extensions == {}


def test_postgres_version_text() -> None:
    assert postgres_version_text(180001) == "18.1"
    assert postgres_version_text(180000) == "18.0"
    assert postgres_version_text(170012) == "17.12"


def test_bodies_contain_only_fixed_codes_not_error_text() -> None:
    # DbUnavailable has no free-text field, so nothing can be smuggled into the body.
    assert [f for f in DbUnavailable.__dataclass_fields__] == ["code"]
    text = json.dumps(evaluate_readiness(DbUnavailable("db_unreachable"), 1)[1].to_body())
    assert "127.0.0.1" not in text
    assert SECRET not in text


# --- probe_database failure mapping ----------------------------------------------------


async def test_probe_reports_db_unreachable_on_connection_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="cairn_core.health")
    started = time.perf_counter()
    result = await probe_database(settings_for(closed_port()))
    assert result == DbUnavailable("db_unreachable")
    assert time.perf_counter() - started < 3
    # Log carries the exception class and SQLSTATE, never the host, port, user or password.
    [record] = [r for r in caplog.records if r.getMessage() == "readyz.probe_failed"]
    assert record.error_class == "OperationalError"  # type: ignore[attr-defined]
    assert record.reason == "db_unreachable"  # type: ignore[attr-defined]
    logged = repr(record.__dict__)
    assert SECRET not in logged
    assert "127.0.0.1" not in logged


async def test_probe_reports_db_timeout_when_the_server_never_answers() -> None:
    with silent_listener() as port:
        started = time.perf_counter()
        result = await probe_database(settings_for(port, timeout_s=0.4))
    assert result == DbUnavailable("db_timeout")
    assert time.perf_counter() - started < 3


async def test_probe_is_repeatable_so_recovery_needs_no_restart() -> None:
    # Nothing is cached between calls: each call opens its own connection.
    settings = settings_for(closed_port())
    assert await probe_database(settings) == DbUnavailable("db_unreachable")
    assert await probe_database(settings) == DbUnavailable("db_unreachable")


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (TimeoutError(), "db_timeout"),
        (psycopg.errors.QueryCanceled("statement timeout"), "db_timeout"),
        (psycopg.OperationalError("connection timeout expired"), "db_timeout"),
        (psycopg.OperationalError("connection refused"), "db_unreachable"),
        (psycopg.OperationalError("password authentication failed"), "db_unreachable"),
        (ConnectionResetError("reset"), "db_unreachable"),
        (psycopg.errors.InsufficientPrivilege("denied"), "probe_failed"),
        (psycopg.DataError("bad"), "probe_failed"),
        (RuntimeError("anything else"), "probe_failed"),
    ],
)
def test_exceptions_map_to_fixed_codes_and_never_leak_text(
    exc: Exception, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    from cairn_core.health import _failure

    caplog.set_level(logging.WARNING, logger="cairn_core.health")
    exc.add_note(f"host=db.internal password={SECRET}")
    assert _failure(exc) == DbUnavailable(code)  # type: ignore[arg-type]
    assert SECRET not in caplog.text
    assert "db.internal" not in caplog.text


# --- TLS on the probe connection (free-tier FT-14, Requirement 4.4) --------------------


@pytest.mark.parametrize(
    ("tls", "expected"),
    [
        ({}, {"sslmode": "prefer"}),
        (
            {"CAIRN_DB_SSLMODE": "verify-full", "CAIRN_DB_SSLROOTCERT": "/certs/root.crt"},
            {"sslmode": "verify-full", "sslrootcert": "/certs/root.crt"},
        ),
    ],
)
async def test_probe_passes_the_tls_settings_to_the_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tls: dict[str, str],
    expected: dict[str, str],
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    for key in ("CAIRN_DB_SSLMODE", "CAIRN_DB_SSLROOTCERT"):
        monkeypatch.delenv(key, raising=False)
    captured: list[dict[str, object]] = []

    async def fake_connect(*_args: object, **kwargs: object) -> None:
        captured.append(kwargs)
        raise psycopg.OperationalError("stopped by the test")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", staticmethod(fake_connect))
    settings = ApiSettings.model_validate(
        {"CAIRN_DB_HOST": "127.0.0.1", "CAIRN_APP_DB_PASSWORD": SECRET, **tls}
    )
    assert await probe_database(settings) == DbUnavailable("db_unreachable")
    (kwargs,) = captured
    assert {k: kwargs[k] for k in ("sslmode", "sslrootcert") if k in kwargs} == expected
    assert kwargs["user"] == "cairn_app"


def test_a_sync_check_appears_only_when_live_sync_is_configured() -> None:
    from cairn_core.health import DbUnavailable, JobsStatus, evaluate_readiness

    down = DbUnavailable("db_unreachable")
    _, plain = evaluate_readiness(down, 1, JobsStatus(launched=True, attempts=1))
    assert "sync" not in plain.checks
    _, waiting = evaluate_readiness(
        down, 1, JobsStatus(launched=True, attempts=1, sync_ready=False)
    )
    assert waiting.checks["sync"].ok is False
    assert waiting.checks["sync"].error == "sync_not_ready"
    _, ready = evaluate_readiness(down, 1, JobsStatus(launched=True, attempts=1, sync_ready=True))
    assert ready.checks["sync"].ok is True
