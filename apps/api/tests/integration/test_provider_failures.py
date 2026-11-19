"""Provider failure paths (plan 1.30; PD1-28; ADR-0015): search survives a failed embedder or
reranker and says so; the answer endpoint returns a fixed 503 or 502 and never an uncited answer;
a configuration fault (auth) is not swallowed."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from cairn_api.main import build_app
from cairn_api.search import build_runtime
from cairn_core.config import ApiSettings
from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus
from cairn_retrieval.cloudflare.client import WorkersAiError
from cairn_retrieval.llm.types import LlmRequest, LlmResponse
from cairn_retrieval.models.fake import FakeEmbedder, FakeReranker
from cairn_retrieval.runtime import build_providers

from .conftest import login, search_settings

pytestmark = pytest.mark.integration

Q = "Why did payment-service move from MongoDB to PostgreSQL?"


class BrokenEmbedder(FakeEmbedder):
    def __init__(self, code: str) -> None:
        super().__init__(384)
        self.code = code

    async def embed_query(self, text: str) -> list[float]:
        raise WorkersAiError(self.code, model="m")  # type: ignore[arg-type]


class BrokenReranker(FakeReranker):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        raise WorkersAiError(self.code, model="m")  # type: ignore[arg-type]


class BrokenLlm:
    provider, model = "fake", "broken"

    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    async def complete(self, req: LlmRequest) -> LlmResponse:
        self.calls += 1
        raise WorkersAiError(self.code, model="m")  # type: ignore[arg-type]


def make_client(db: FreshDb, **replace: Any) -> Iterator[TestClient]:
    api = ApiSettings.model_validate({"CAIRN_APP_DB_PASSWORD": "x", "CAIRN_PROXY_TOKEN": None})
    search = search_settings()
    providers = build_providers(search)
    for name, value in replace.items():
        setattr(providers, name, value)
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        db.app_dsn, min_size=1, max_size=4, kwargs={"prepare_threshold": None}, open=False
    )
    with TestClient(build_app(api, search=build_runtime(api, search, providers, pool=pool))) as c:
        yield c


def test_a_failed_embedding_degrades_to_full_text(seeded: tuple[FreshDb, SeededCorpus]) -> None:
    for code in ("budget_exhausted", "daily_limit", "server", "timeout"):
        for client in make_client(seeded[0], embedder=BrokenEmbedder(code)):
            login(client, "asha")
            body = client.post("/search", json={"query": Q}).json()
            assert body["degraded"] == ["dense"], code
            assert body["results"], "full-text candidates must still come back"
            ans = client.post("/answer", json={"question": Q}).json()
            assert ans["degraded"] == ["dense"]
            assert not ans["abstained"]


def test_a_failed_rerank_keeps_the_fused_order(seeded: tuple[FreshDb, SeededCorpus]) -> None:
    for client in make_client(seeded[0], reranker=BrokenReranker("rate_limited")):
        login(client, "asha")
        body = client.post("/search", json={"query": Q}).json()
        assert body["degraded"] == ["rerank"]
        assert body["results"]


def test_a_configuration_fault_is_not_swallowed(seeded: tuple[FreshDb, SeededCorpus]) -> None:
    for client in make_client(seeded[0], embedder=BrokenEmbedder("auth")):
        login(client, "asha")
        r = client.post("/search", json={"query": Q})
        assert r.status_code == 503
        assert r.json() == {"error": "models_unavailable"}


def test_an_exhausted_answer_budget_is_a_fixed_503_with_retry_after(
    seeded: tuple[FreshDb, SeededCorpus],
) -> None:
    llm = BrokenLlm("budget_exhausted")
    for client in make_client(seeded[0], answer=llm):
        login(client, "asha")
        r = client.post("/answer", json={"question": Q})
        assert r.status_code == 503
        assert r.json() == {"error": "model_budget_exhausted"}
        assert int(r.headers["retry-after"]) > 0
        assert "answer" not in r.json()
    assert llm.calls == 1


def test_another_llm_failure_is_a_502(seeded: tuple[FreshDb, SeededCorpus]) -> None:
    for client in make_client(seeded[0], answer=BrokenLlm("server")):
        login(client, "asha")
        r = client.post("/answer", json={"question": Q})
        assert r.status_code == 502
        assert r.json() == {"error": "llm_unavailable"}


def test_nothing_visible_means_no_model_call(seeded: tuple[FreshDb, SeededCorpus]) -> None:
    llm = BrokenLlm("server")
    for client in make_client(seeded[0], answer=llm):
        login(client, "ben")
        body = client.post("/answer", json={"question": "zzqx wvvk plorb"}).json()
        assert body["abstained"] is True
    assert llm.calls == 0  # an empty pack never reaches the model
