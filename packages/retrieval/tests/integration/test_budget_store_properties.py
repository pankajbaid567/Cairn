"""Model-based property test: the Postgres usage store against the in-memory store.

Feature: cairn-free-tier-retarget, Property 21: The Postgres usage store behaves like the
in-memory model
**Validates: Requirements 15.7, 15.5**

For any cap and any sequence of reservation requests (day, size), the in-memory store is the
model. Running the same sequence against ``PostgresUsageStore`` on the session's fresh test
database must give the same answer to every request (the new running total, or ``None`` for a
refusal) and the same total per day at the end. Sizes run from 0 to above the cap, so refusals,
exact fits, zero-neuron reservations and reservations larger than the cap are all exercised.

Needs the compose db (``make up-db``); skips otherwise. 25 examples per run, because each request
opens a real connection (design property notes). Every example uses its own provider name, so
examples never see each other's rows in the shared database.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_core.config import ApiSettings, libpq_tls
from cairn_core.testing import DbTestConfig, FreshDb
from cairn_retrieval.cloudflare.budget import InMemoryUsageStore, PostgresUsageStore

pytestmark = [pytest.mark.integration, pytest.mark.managed]

BASE_DAY = date(2031, 1, 1)
_example_counter = itertools.count()

CAPS = st.integers(min_value=0, max_value=60)
# (day offset, size): three days so per-day totals stay separate; sizes reach past the largest cap.
REQUESTS = st.lists(
    st.tuples(st.integers(min_value=0, max_value=2), st.integers(min_value=0, max_value=70)),
    min_size=1,
    max_size=15,
)


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


async def run_sequence(
    store: PostgresUsageStore | InMemoryUsageStore,
    provider: str,
    cap: int,
    requests: list[tuple[int, int]],
) -> tuple[list[int | None], dict[int, int]]:
    """Answers to each request in order, and the total of each day at the end."""
    answers = [
        await store.try_reserve(BASE_DAY + timedelta(days=offset), provider, size, cap)
        for offset, size in requests
    ]
    totals = {
        offset: await store.used(BASE_DAY + timedelta(days=offset), provider) for offset in range(3)
    }
    return answers, totals


def test_the_postgres_store_behaves_like_the_in_memory_model(
    fresh_db: FreshDb, admin_config: DbTestConfig
) -> None:
    postgres = store_for(fresh_db, admin_config)

    @settings(max_examples=25, deadline=None)
    @given(cap=CAPS, requests=REQUESTS)
    def check(cap: int, requests: list[tuple[int, int]]) -> None:
        provider = f"prop21_{next(_example_counter)}"
        model_answers, model_totals = asyncio.run(
            run_sequence(InMemoryUsageStore(), provider, cap, requests)
        )
        real_answers, real_totals = asyncio.run(run_sequence(postgres, provider, cap, requests))
        assert real_answers == model_answers
        assert real_totals == model_totals
        assert all(total <= cap for total in real_totals.values())

    check()
