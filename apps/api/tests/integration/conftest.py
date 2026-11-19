"""Fixtures for API integration tests: the real routes, RLS, a seeded database and the fakes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from cairn_api.main import build_app
from cairn_api.search import build_runtime
from cairn_api.sync import ingest_inbox_writer
from cairn_core.config import ApiSettings, SearchSettings
from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus
from cairn_retrieval.runtime import build_providers

SECRET = "s" * 40


def search_settings(**overrides: Any) -> SearchSettings:
    values: dict[str, Any] = {
        "CAIRN_ENV": "local",
        "CAIRN_SESSION_SECRET": SECRET,
        "CAIRN_AUDIT_HASH_KEY": "a" * 40,
        "CAIRN_EMBEDDER": "fake",
        "CAIRN_RERANKER": "fake",
        "CAIRN_ANSWER_PROVIDER": "fake",
        "CAIRN_JUDGE_PROVIDER": "fake",
        "CAIRN_RERANK_MIN_SCORE": 0.001,  # the fake reranker scores 0 without shared words
        "CAIRN_PROXY_TOKEN": None,
        **overrides,
    }
    return SearchSettings.model_validate(values)


def make_app_client(db: FreshDb, **overrides: Any) -> Iterator[TestClient]:
    api = ApiSettings.model_validate({"CAIRN_APP_DB_PASSWORD": "unused", "CAIRN_PROXY_TOKEN": None})
    search = search_settings(**overrides)
    webhook = search.github_webhook_secret is not None
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        db.app_dsn, min_size=1, max_size=4, kwargs={"prepare_threshold": None}, open=False
    )
    runtime = build_runtime(api, search, build_providers(search), pool=pool)
    if webhook:
        # the local receiver stores through the throwaway database's ingest login, not the env's
        import psycopg

        async def connect() -> Any:
            assert db.ingest_dsn is not None
            return await psycopg.AsyncConnection.connect(db.ingest_dsn, prepare_threshold=None)

        runtime.inbox_writer = ingest_inbox_writer(connect)
    app = build_app(api, probe=None, search=runtime)
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def client(seeded: tuple[FreshDb, SeededCorpus]) -> Iterator[TestClient]:
    yield from make_app_client(seeded[0])


def login(client: TestClient, persona: str) -> None:
    client.cookies.clear()
    r = client.post("/auth/dev-login", json={"persona": persona})
    assert r.status_code == 204, r.text


@pytest.fixture(scope="session")
def decisions(seeded: tuple[FreshDb, SeededCorpus]) -> Iterator[tuple[FreshDb, SeededCorpus]]:
    """The seeded corpus plus the Kestrel Pay gold decisions, loaded once."""
    from cairn_eval.decision_seed import seed_decisions

    db, corpus = seeded
    with db.connect_ingest() as conn:
        seed_decisions(conn, corpus.scenario, corpus.manifest)
    yield db, corpus  # noqa: PT022
