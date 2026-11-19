"""Property test for readiness body hygiene (task 5.3).

Feature: cairn-phase-0-foundations, Property 3: Readiness bodies carry codes, never error detail
**Validates: Requirements 7.3, 7.8**

For any probe failure whose exception text, notes and cause chain hold arbitrary strings,
including host names, DSNs and passwords, the serialized readiness body holds only the fixed
error codes and none of those strings. Three routes into the body are covered:

* ``_failure(exc)`` followed by ``evaluate_readiness``: the mapping from exception to code.
* ``probe_database`` with ``psycopg.AsyncConnection.connect`` raising: the real failure path,
  with the settings' host and password among the strings that must stay out of the body and
  out of the log record the probe writes.
* ``GET /readyz`` through the ASGI app with a probe that raises, and with a probe that returns
  the mapped failure: the HTTP body, not only the report object.

Generated strings are at least 8 characters and are filtered so they cannot be a piece of the
fixed body text; the check also looks for their JSON-escaped forms, since a body is serialized.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any
from unittest import mock

import psycopg
import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_api.main import build_app
from cairn_core.config import ApiSettings
from cairn_core.health import (
    DbUnavailable,
    ProbeResult,
    _failure,
    evaluate_readiness,
    probe_database,
)

UNAVAILABLE_CODES = frozenset({"db_unreachable", "db_timeout", "probe_failed"})
BODY_KEYS = {"status", "checks", "extensions"}

# Everything the body can say when the database cannot be probed.
BASELINE_TEXT = json.dumps(
    evaluate_readiness(DbUnavailable("db_unreachable"), 1)[1].to_body()
) + " ".join(UNAVAILABLE_CODES)


STANDARD_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def variants(secret: str) -> set[str]:
    """The secret as written, and as it would appear inside a JSON string."""
    return {
        secret,
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
    }


def usable(secret: str) -> bool:
    """Long enough, not blank, and not a piece of the fixed body or log text in any spelling."""
    return len(secret.strip()) >= 8 and not any(
        v in BASELINE_TEXT or v in LOG_BASELINE for v in variants(secret)
    )


free_text = st.text(min_size=8, max_size=60).filter(usable)
hostnames = st.from_regex(r"[a-z][a-z0-9-]{6,20}\.(internal|corp|example)", fullmatch=True).filter(
    usable
)
passwords = st.text(
    alphabet=st.characters(codec="ascii", categories=("L", "N", "P", "S")),
    min_size=8,
    max_size=32,
).filter(usable)

# Exception types a connection attempt or a query can end in, plus ones the probe never expects.
ExcFactory = Callable[[str], BaseException]
EXCEPTION_FACTORIES: list[ExcFactory] = [
    TimeoutError,
    lambda m: OSError(111, m),
    ConnectionResetError,
    psycopg.OperationalError,
    psycopg.errors.QueryCanceled,
    psycopg.errors.InsufficientPrivilege,
    psycopg.errors.UndefinedTable,
    psycopg.DataError,
    RuntimeError,
    ValueError,
    KeyError,
    lambda m: Exception(m, m[::-1], {"detail": m}),
]


# What the probe's log line may say: a fixed message, three fixed keys, a class name, a code.
LOG_BASELINE = " ".join(
    ["readyz.probe_failed", "error_class", "sqlstate", "reason", "None", *UNAVAILABLE_CODES]
    + [type(make("x")).__name__ for make in EXCEPTION_FACTORIES]
)


@st.composite
def failures(draw: st.DrawFn) -> tuple[BaseException, list[str]]:
    """An exception whose text, note and cause carry secrets, with those secrets listed."""
    text, host, password = draw(free_text), draw(hostnames), draw(passwords)
    dsn = f"postgresql://cairn_app:{password}@{host}:5432/cairn"
    message = f"{text} host={host} password={password} dsn={dsn}"
    exc = draw(st.sampled_from(EXCEPTION_FACTORIES))(message)
    if draw(st.booleans()):
        exc.add_note(f"note: {dsn}")
    if draw(st.booleans()):
        exc.__cause__ = RuntimeError(f"cause: {message}")
    return exc, [text, host, password, dsn, message]


def assert_absent(secrets: list[str], *texts: str) -> None:
    for secret in secrets:
        for variant in variants(secret):
            for text in texts:
                assert variant not in text


def assert_only_codes(status: int, body: dict[str, Any]) -> None:
    """Shape of the body when the database could not be probed: fixed keys, one fixed code."""
    assert status == 503
    assert set(body) == BODY_KEYS
    assert body["status"] == "not_ready"
    assert body["extensions"] == {}
    assert set(body["checks"]) == {"database"}
    database = body["checks"]["database"]
    assert set(database) == {"ok", "error"}
    assert database["ok"] is False
    assert database["error"] in UNAVAILABLE_CODES


def settings_for(host: str, password: str) -> ApiSettings:
    return ApiSettings.model_validate(
        {
            "CAIRN_DB_HOST": host,
            "CAIRN_DB_PORT": 5432,
            "CAIRN_DB_TIMEOUT_S": 2.0,
            "CAIRN_APP_DB_PASSWORD": password,
            "CAIRN_PROXY_TOKEN": None,  # explicit: a local .env may hold a token
        }
    )


class RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.dumps: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        # The message and the fields added through ``extra``; not pid, times or file paths.
        extras = {k: v for k, v in record.__dict__.items() if k not in STANDARD_RECORD_ATTRS}
        self.dumps.append(f"{record.getMessage()} {extras!r}")


# --- _failure -> evaluate_readiness ------------------------------------------------------


@given(failure=failures())
def test_mapped_failures_give_a_body_with_only_a_fixed_code(
    failure: tuple[BaseException, list[str]],
) -> None:
    exc, secrets = failure
    status, report = evaluate_readiness(_failure(exc), 1)
    body = report.to_body()
    assert_only_codes(status, body)
    assert_absent(secrets, json.dumps(body), json.dumps(body, ensure_ascii=False))


# --- probe_database failure path ----------------------------------------------------------


@settings(deadline=None)
@given(failure=failures(), host=hostnames, password=passwords)
def test_probe_database_failures_leak_nothing_into_the_body_or_the_log(
    failure: tuple[BaseException, list[str]], host: str, password: str
) -> None:
    exc, secrets = failure

    async def failing_connect(*args: object, **kwargs: object) -> None:
        raise exc

    collector = RecordCollector()
    logger = logging.getLogger("cairn_core.health")
    logger.addHandler(collector)
    try:
        with mock.patch.object(psycopg.AsyncConnection, "connect", new=failing_connect):
            result = asyncio.run(probe_database(settings_for(host, password)))
    finally:
        logger.removeHandler(collector)

    assert isinstance(result, DbUnavailable)
    status, report = evaluate_readiness(result, 1)
    body = report.to_body()
    assert_only_codes(status, body)
    everything = [*secrets, host, password]
    assert_absent(everything, json.dumps(body), json.dumps(body, ensure_ascii=False))
    assert len(collector.dumps) == 1  # the failure is logged once, and without any detail
    assert_absent(everything, *collector.dumps)


# --- GET /readyz --------------------------------------------------------------------------


class SwitchableProbe:
    """One app for every example; each example sets what the probe does next."""

    def __init__(self) -> None:
        self.raises: BaseException | None = None
        self.returns: ProbeResult | None = None

    async def __call__(self) -> ProbeResult:
        if self.raises is not None:
            raise self.raises
        assert self.returns is not None
        return self.returns


@pytest.fixture(scope="module")
def probe_switch() -> SwitchableProbe:
    return SwitchableProbe()


@pytest.fixture(scope="module")
def readyz_client(probe_switch: SwitchableProbe) -> Iterator[TestClient]:
    app = build_app(
        settings_for("db.example", "unused-password"), probe=probe_switch, expected_head=1
    )
    with TestClient(app) as client:
        yield client


@settings(deadline=None)
@given(failure=failures())
def test_readyz_body_never_carries_text_of_a_crashing_probe(
    readyz_client: TestClient,
    probe_switch: SwitchableProbe,
    failure: tuple[BaseException, list[str]],
) -> None:
    exc, secrets = failure
    probe_switch.raises, probe_switch.returns = exc, None
    response = readyz_client.get("/readyz")
    body = response.json()
    assert_only_codes(response.status_code, body)
    assert body["checks"]["database"]["error"] == "probe_failed"
    assert_absent(secrets, response.text, json.dumps(body, ensure_ascii=False))


@settings(deadline=None)
@given(failure=failures())
def test_readyz_body_never_carries_text_of_a_mapped_failure(
    readyz_client: TestClient,
    probe_switch: SwitchableProbe,
    failure: tuple[BaseException, list[str]],
) -> None:
    exc, secrets = failure
    probe_switch.raises, probe_switch.returns = None, _failure(exc)
    response = readyz_client.get("/readyz")
    body = response.json()
    assert_only_codes(response.status_code, body)
    assert_absent(secrets, response.text, json.dumps(body, ensure_ascii=False))
