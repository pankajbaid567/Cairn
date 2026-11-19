"""Extraction cache and alias resolution against a real database (plan 2.3, 2.8)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from cairn_connectors.decisions import aliases
from cairn_connectors.decisions.extraction import PROMPT_VERSION, extract
from cairn_core.testing import DbTestConfig, FreshDb, temporary_database
from cairn_retrieval.llm.types import LlmRequest, LlmResponse

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> Iterator[psycopg.Connection[Any]]:
    if admin_config.settings.ingest_db_password is None:
        pytest.skip("CAIRN_INGEST_DB_PASSWORD is not set (make env-update)")
    with temporary_database(admin_conn, admin_config) as db:
        assert isinstance(db, FreshDb)
        assert db.ingest_dsn is not None
        with psycopg.connect(db.ingest_dsn, prepare_threshold=None) as c:
            yield c


class Scripted:
    provider, model = "fake", "scripted-v1"

    def __init__(self, reply: str) -> None:
        self.reply, self.calls = reply, 0

    async def complete(self, req: LlmRequest) -> LlmResponse:
        self.calls += 1
        return LlmResponse(
            text=self.reply,
            tokens_in=1,
            tokens_out=1,
            latency_ms=0.0,
            provider="fake",
            model=self.model,
        )


REPLY = json.dumps(
    {
        "decisions": [
            {
                "key": None,
                "title": "T",
                "question": None,
                "outcome": "O",
                "status": "accepted",
                "options": [],
                "evidence": [{"start": 0, "end": 5}],
            }
        ]
    }
)
HASH = "a" * 64


def test_extraction_is_cached_by_revision_model_and_prompt_version(
    conn: psycopg.Connection[Any],
) -> None:
    llm = Scripted(REPLY)
    first = asyncio.run(extract(conn, llm, HASH, "hello world"))
    second = asyncio.run(extract(conn, llm, HASH, "hello world"))
    assert llm.calls == 1
    assert not first.cached
    assert second.cached
    assert [d.title for d in second.decisions] == ["T"]
    asyncio.run(extract(conn, llm, "b" * 64, "hello world"))  # a different revision is a new call
    assert llm.calls == 2
    row = conn.execute(
        "SELECT count(*) FROM cairn.extractions WHERE prompt_version = %s", (PROMPT_VERSION,)
    ).fetchone()
    assert row is not None
    assert row[0] == 2


def test_alias_resolution_is_deterministic_first_and_reviewable_after(
    conn: psycopg.Connection[Any],
) -> None:
    ent = aliases.ensure_entity(conn, "service", "payment-service")
    assert aliases.ensure_entity(conn, "service", "payment-service") == ent  # exact
    assert (
        aliases.resolve(conn, "service", "Payment Service") == ent
    )  # normalized, recorded as a confirmed alias
    row = conn.execute(
        "SELECT method, confirmed FROM cairn.aliases WHERE alias = 'Payment Service'"
    ).fetchone()
    assert row == ("normalized", True)
    assert aliases.resolve(conn, "service", "billing") is None
    assert aliases.resolve(conn, "team", "payment-service") is None  # kinds do not mix


def test_embedding_matches_need_confirmation_and_disagreement_opens_a_review(
    conn: psycopg.Connection[Any],
) -> None:
    ent = aliases.ensure_entity(conn, "service", "ledger")

    async def yes(_a: str, _b: str) -> bool:
        return True

    async def no(_a: str, _b: str) -> bool:
        return False

    assert (
        asyncio.run(aliases.resolve_fuzzy(conn, "service", "ledgr", [(ent, "ledger", 0.95)], yes))
        == ent
    )
    assert conn.execute("SELECT confirmed FROM cairn.aliases WHERE alias = 'ledgr'").fetchone() == (
        True,
    )
    assert (
        asyncio.run(
            aliases.resolve_fuzzy(conn, "service", "billing-ledger", [(ent, "ledger", 0.9)], no)
        )
        is None
    )
    assert conn.execute(
        "SELECT confirmed FROM cairn.aliases WHERE alias = 'billing-ledger'"
    ).fetchone() == (False,)
    reviews = conn.execute(
        "SELECT count(*) FROM cairn.review_items WHERE kind = 'alias'"
    ).fetchone()
    assert reviews is not None
    assert reviews[0] == 1
    # below the threshold nothing is created and nothing is asked
    assert (
        asyncio.run(
            aliases.resolve_fuzzy(conn, "service", "unrelated", [(ent, "ledger", 0.3)], yes)
        )
        is None
    )
    # without a confirming model, the match stays unconfirmed and reviewable
    assert (
        asyncio.run(aliases.resolve_fuzzy(conn, "service", "led", [(ent, "ledger", 0.9)], None))
        is None
    )
    assert conn.execute("SELECT confirmed FROM cairn.aliases WHERE alias = 'led'").fetchone() == (
        False,
    )
