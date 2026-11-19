"""``/readyz`` against a real database (Requirements 7.2, 7.3, 7.9, 7.10, 7.12, 9.6).

The app is the real ``build_app`` with the real probe, called in process through httpx's ASGI
transport. The database is the compose ``db`` service (``make up-db``): the ``fresh_db``
fixture gives a migrated ``cairn_test_<hex>`` database and the probe connects to it as
``cairn_app``. Nothing here touches the ``cairn`` database.
"""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Iterator
from contextlib import closing
from typing import Any

import httpx
import psycopg
import pytest
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from psycopg.conninfo import make_conninfo

from cairn_api.main import build_app
from cairn_core.bootstrap import OWNER_ROLE, REQUIRED_EXTENSIONS, bootstrap_database, content_spec
from cairn_core.config import ApiSettings, libpq_tls
from cairn_core.migrate import bundled_migrations, head, migrate
from cairn_core.testing import (
    TEST_DB_PREFIX,
    DbTestConfig,
    FreshDb,
    drop_database,
    temporary_database,
)

pytestmark = pytest.mark.integration

BUDGET_S = 3.0
"""Requirement 7.4: an unreachable database is reported within 3 seconds."""


def api_settings(
    config: DbTestConfig, *, name: str, port: int | None = None, timeout_s: float = 2.0
) -> ApiSettings:
    """Settings for the API pointed at database ``name``, built from the aliases the process
    environment would use (the same keys compose sets)."""
    return ApiSettings.model_validate(
        {
            "CAIRN_DB_HOST": config.target.host,
            "CAIRN_DB_PORT": config.target.port if port is None else port,
            "CAIRN_DB_NAME": name,
            "CAIRN_DB_TIMEOUT_S": timeout_s,
            "CAIRN_APP_DB_PASSWORD": config.settings.app_db_password.get_secret_value(),
            "CAIRN_PROXY_TOKEN": None,  # explicit: a local .env may hold a token
        }
    )


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def unused_port() -> int:
    """A port that was free a moment ago. Nothing listens on it, so connecting is refused."""
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def assert_body_hygiene(response: httpx.Response, config: DbTestConfig) -> None:
    """Codes only: no password, host, port or exception text anywhere in the body."""
    text = response.text
    assert config.settings.app_db_password.get_secret_value() not in text
    assert str(config.target.port) not in text
    assert "psycopg" not in text
    assert "Traceback" not in text
    assert set(response.json()) == {"status", "checks", "extensions"}
    assert response.headers["cache-control"] == "no-store"


async def test_ready_against_a_migrated_database(
    fresh_db: FreshDb, admin_config: DbTestConfig
) -> None:
    app = build_app(api_settings(admin_config, name=fresh_db.name))
    async with client_for(app) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"database", "postgres", "extensions", "role", "schema"}
    assert all(check["ok"] for check in body["checks"].values())
    assert body["checks"]["role"]["detail"] == {"name": "cairn_app"}
    assert body["checks"]["postgres"]["detail"]["version"].startswith("18.")
    expected = head(bundled_migrations())
    assert body["checks"]["schema"]["detail"] == {"version": expected, "expected": expected}
    # The two extension versions are the installed ones, not a fixed string.
    assert set(body["extensions"]) == set(REQUIRED_EXTENSIONS)
    assert all(version for version in body["extensions"].values())
    assert_body_hygiene(response, admin_config)


async def test_healthz_does_not_need_the_database(admin_config: DbTestConfig) -> None:
    app = build_app(api_settings(admin_config, name="cairn", port=unused_port()))
    async with client_for(app) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_closed_port_is_503_db_unreachable_within_budget(
    admin_config: DbTestConfig,
) -> None:
    app = build_app(api_settings(admin_config, name="cairn", port=unused_port()))
    async with client_for(app) as client:
        started = time.perf_counter()
        response = await client.get("/readyz")
        elapsed = time.perf_counter() - started

    assert response.status_code == 503
    assert elapsed < BUDGET_S
    body = response.json()
    assert body == {
        "status": "not_ready",
        "checks": {"database": {"ok": False, "error": "db_unreachable"}},
        "extensions": {},
    }
    assert_body_hygiene(response, admin_config)


async def test_schema_behind_is_503_with_codes_only_then_200_once_migrated(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """A bootstrapped database with no migrations applied is reachable but not ready; after
    the migrations run, the same app instance reports ready (no state is kept between calls)."""
    with temporary_database(admin_conn, admin_config, apply_migrations=False) as db:
        app = build_app(api_settings(admin_config, name=db.name))
        async with client_for(app) as client:
            behind = await client.get("/readyz")
            with db.connect_owner() as owner:
                migrate(owner, bundled_migrations())
            ready = await client.get("/readyz")

    assert behind.status_code == 503
    behind_body = behind.json()
    assert behind_body["status"] == "not_ready"
    assert behind_body["checks"]["database"]["ok"] is True
    assert behind_body["checks"]["schema"]["error"] == "schema_behind"
    assert behind_body["checks"]["schema"]["detail"] == {
        "version": 0,
        "expected": head(bundled_migrations()),
    }
    assert_body_hygiene(behind, admin_config)
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


async def test_outage_then_recovery_when_the_database_appears(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """The database does not exist at the first call (503 ``db_unreachable``: the server
    refuses the connection) and is created and migrated before the second (200)."""
    name = TEST_DB_PREFIX + "outage" + str(time.time_ns())[-6:]
    app = build_app(api_settings(admin_config, name=name))
    try:
        async with client_for(app) as client:
            down = await client.get("/readyz")

            bootstrap_database(admin_conn, name, spec=content_spec(name))
            owner_dsn = make_conninfo(
                host=admin_config.target.host,
                port=admin_config.target.port,
                dbname=name,
                user=OWNER_ROLE,
                password=admin_config.settings.owner_db_password.get_secret_value(),
                **libpq_tls(admin_config.settings),
            )
            with psycopg.connect(owner_dsn, autocommit=True) as owner:
                migrate(owner, bundled_migrations())

            up = await client.get("/readyz")
    finally:
        drop_database(admin_conn, name)

    assert down.status_code == 503
    assert down.json()["checks"] == {"database": {"ok": False, "error": "db_unreachable"}}
    assert_body_hygiene(down, admin_config)
    assert up.status_code == 200
    assert up.json()["status"] == "ready"


@pytest.fixture
def psycopg_instrumented() -> Iterator[None]:
    """The psycopg instrumentation is a process-wide patch; undo it after the test."""
    yield
    PsycopgInstrumentor().uninstrument()


async def test_readyz_records_a_server_span_and_client_spans_for_its_queries(
    fresh_db: FreshDb, admin_config: DbTestConfig, psycopg_instrumented: None
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    PsycopgInstrumentor().instrument(tracer_provider=provider)
    app = build_app(api_settings(admin_config, name=fresh_db.name), tracer_provider=provider)

    async with client_for(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    servers = [s for s in spans if s.kind == SpanKind.SERVER]
    clients = [s for s in spans if s.kind == SpanKind.CLIENT]
    assert len(servers) == 1
    assert servers[0].name.startswith("GET")
    # One client span per statement the probe sends (the probe statement and max(version)).
    assert len(clients) == 2
    server_ctx = servers[0].get_span_context()
    assert server_ctx is not None
    for span in clients:
        assert span.context is not None
        assert span.context.trace_id == server_ctx.trace_id
        assert span.parent is not None
        assert span.parent.span_id == server_ctx.span_id
        attributes = span.attributes or {}
        assert attributes.get("db.system") == "postgresql"
        # The password must not reach a span attribute.
        assert admin_config.settings.app_db_password.get_secret_value() not in json.dumps(
            {k: str(v) for k, v in attributes.items()}
        )
