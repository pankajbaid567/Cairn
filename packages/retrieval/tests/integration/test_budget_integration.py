"""Postgres usage store and migration 0002 against the compose db (needs `make up-db`).

Also runs against ``ft-db`` with ``CAIRN_TEST_ADMIN_MODE=managed`` (`make matrix-managed`).
Each test uses its own day, so tests that share the session's ``fresh_db`` never meet.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import psycopg
import pytest

from cairn_core.config import ApiSettings, libpq_tls
from cairn_core.testing import DbTestConfig, FreshDb
from cairn_retrieval.cloudflare.budget import (
    BudgetExhausted,
    InMemoryUsageStore,
    NeuronBudget,
    PostgresUsageStore,
)

pytestmark = [pytest.mark.integration, pytest.mark.managed]

PROVIDER = "workers_ai"


def store_for(db: FreshDb, config: DbTestConfig) -> PostgresUsageStore:
    """A store that connects to ``db`` as ``cairn_app`` with the test run's TLS settings."""
    values: dict[str, Any] = {
        "CAIRN_DB_HOST": db.host,
        "CAIRN_DB_PORT": db.port,
        "CAIRN_DB_NAME": db.name,
        "CAIRN_APP_DB_PASSWORD": config.settings.app_db_password.get_secret_value(),
        "CAIRN_DB_SSLMODE": libpq_tls(config.settings)["sslmode"],
    }
    rootcert = libpq_tls(config.settings).get("sslrootcert")
    if rootcert is not None:
        values["CAIRN_DB_SSLROOTCERT"] = rootcert
    return PostgresUsageStore(ApiSettings.model_validate(values))


def test_the_migration_is_recorded_and_the_app_role_has_exactly_three_privileges(
    fresh_db: FreshDb,
) -> None:
    """15.6: SELECT, INSERT and UPDATE for cairn_app; nothing for anyone else.

    Reads as ``cairn_owner``: in managed mode the admin has no privilege on schema cairn_meta.
    """
    with fresh_db.connect_owner() as conn:
        row = conn.execute(
            "SELECT name FROM cairn_meta.schema_migrations WHERE version = 2"
        ).fetchone()
        assert row == ("provider_usage",)
        owner = conn.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid = 'cairn_meta.provider_usage'::regclass"
        ).fetchone()
        assert owner == ("cairn_owner",)

        def has(privilege: str) -> bool:
            result = conn.execute(
                "SELECT has_table_privilege('cairn_app', 'cairn_meta.provider_usage', %s)",
                (privilege,),
            ).fetchone()
            assert result is not None
            return bool(result[0])

        assert [has(p) for p in ("SELECT", "INSERT", "UPDATE")] == [True, True, True]
        assert [has(p) for p in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")] == [False] * 4
        public = conn.execute(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE table_schema = 'cairn_meta' AND table_name = 'provider_usage' "
            "AND grantee = 'PUBLIC'"
        ).fetchone()
        assert public == (0,)


def test_the_app_role_cannot_delete_a_reservation(fresh_db: FreshDb) -> None:
    with fresh_db.connect_app() as app, pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute("DELETE FROM cairn_meta.provider_usage")


def test_the_table_refuses_a_bad_provider_and_a_negative_total(fresh_db: FreshDb) -> None:
    with fresh_db.connect_app() as app:
        with pytest.raises(psycopg.errors.CheckViolation):
            app.execute(
                "INSERT INTO cairn_meta.provider_usage (day, provider) VALUES ('2030-02-01', 'Bad')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            app.execute(
                "INSERT INTO cairn_meta.provider_usage (day, provider, neurons_reserved) "
                "VALUES ('2030-02-01', 'workers_ai', -1)"
            )


async def test_reservations_persist_across_store_instances(
    fresh_db: FreshDb, admin_config: DbTestConfig
) -> None:
    """15.5: a restarted process sees the same total, and a refusal changes nothing."""
    day = date(2030, 3, 1)
    assert await store_for(fresh_db, admin_config).try_reserve(day, PROVIDER, 60, 100) == 60
    second = store_for(fresh_db, admin_config)  # a new process, in effect
    assert await second.used(day, PROVIDER) == 60
    assert await second.try_reserve(day, PROVIDER, 40, 100) == 100
    assert await second.try_reserve(day, PROVIDER, 1, 100) is None
    assert await second.used(day, PROVIDER) == 100
    assert await second.used(date(2030, 3, 2), PROVIDER) == 0


async def test_a_reservation_larger_than_the_cap_inserts_nothing(
    fresh_db: FreshDb, admin_config: DbTestConfig
) -> None:
    day = date(2030, 4, 1)
    store = store_for(fresh_db, admin_config)
    assert await store.try_reserve(day, PROVIDER, 101, 100) is None
    with fresh_db.connect_owner() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM cairn_meta.provider_usage WHERE day = %s", (day,)
        ).fetchone()
    assert rows == (0,)


async def test_twenty_concurrent_reservations_never_pass_the_cap(
    fresh_db: FreshDb, admin_config: DbTestConfig
) -> None:
    """15.5: 20 sessions race on a day with no row yet; 14 of 7 neurons fit under 100."""
    day = date(2030, 5, 1)
    store = store_for(fresh_db, admin_config)
    results = await asyncio.gather(*(store.try_reserve(day, PROVIDER, 7, 100) for _ in range(20)))
    accepted = sorted(r for r in results if r is not None)
    assert accepted == [7 * i for i in range(1, 15)]  # every running total once, none above 98
    assert results.count(None) == 6
    assert await store.used(day, PROVIDER) == 98


async def test_the_postgres_and_memory_stores_agree_on_a_fixed_sequence(
    fresh_db: FreshDb, admin_config: DbTestConfig
) -> None:
    """15.7 in miniature (the property test is task 8.6): same answers, same totals."""
    day = date(2030, 6, 1)
    postgres = NeuronBudget(store_for(fresh_db, admin_config), 50, today=lambda: day)
    memory = NeuronBudget(InMemoryUsageStore(), 50, today=lambda: day)
    for n in (20, 20, 11, 10, 0, 1, 51):
        outcomes: list[int | None] = []
        for budget in (postgres, memory):
            try:
                outcomes.append(await budget.reserve(n))
            except BudgetExhausted:
                outcomes.append(None)
        assert outcomes[0] == outcomes[1], n
    assert await postgres.used() == await memory.used() == 50
