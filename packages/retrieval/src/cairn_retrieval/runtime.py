"""Build the model providers from settings (design §17 lifespan; PD1-12, PD1-13, PD1-27).

One place for the API lifespan, the importer's ``--embed-inline``, the embedding worker and the
eval tools. Providers are wrapped in per-model concurrency limits. Nothing here loads a model:
embeddings, rerank and answers go to Workers AI over HTTPS, or to the deterministic fakes.
"""

from __future__ import annotations

from dataclasses import dataclass

from cairn_core.config import SearchSettings
from cairn_retrieval.bedrock.client import BedrockClient
from cairn_retrieval.cloudflare.budget import InMemoryUsageStore, NeuronBudget, UsageStore
from cairn_retrieval.cloudflare.client import WorkersAiClient
from cairn_retrieval.llm.factory import build_llm
from cairn_retrieval.llm.types import LlmProvider
from cairn_retrieval.models.factory import build_embedder, build_reranker
from cairn_retrieval.models.limits import limited
from cairn_retrieval.models.protocols import Embedder, Reranker


@dataclass
class Providers:
    embedder: Embedder
    reranker: Reranker | None
    answer: LlmProvider
    judge: LlmProvider
    budget: NeuronBudget | None
    client: WorkersAiClient | None
    bedrock: BedrockClient | None = None
    bedrock_budget: NeuronBudget | None = None

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
        if self.bedrock is not None:
            await self.bedrock.aclose()


def uses_workers_ai(s: SearchSettings) -> bool:
    return "workers_ai" in (s.embedder, s.reranker, s.answer_provider, s.judge_provider)


def uses_bedrock(s: SearchSettings) -> bool:
    return "bedrock" in (s.embedder, s.reranker, s.answer_provider, s.judge_provider)


def build_providers(s: SearchSettings, usage_store: UsageStore | None = None) -> Providers:
    """Embedder, reranker, answer and judge models for ``s``.

    ``usage_store`` backs the neuron budget (Postgres in the combined process, memory in tests);
    it is ignored when no provider is Workers AI.
    """
    budget: NeuronBudget | None = None
    client: WorkersAiClient | None = None
    if uses_workers_ai(s):
        budget = NeuronBudget(usage_store or InMemoryUsageStore(), s.daily_neurons)
        client = WorkersAiClient(s, budget)
    bedrock: BedrockClient | None = None
    bedrock_budget: NeuronBudget | None = None
    if uses_bedrock(s):
        # micro-dollars in the same usage store, under the provider name "bedrock"
        bedrock_budget = NeuronBudget(
            usage_store or InMemoryUsageStore(), s.bedrock_daily_micro_usd, provider="bedrock"
        )
        bedrock = BedrockClient(s, bedrock_budget)
    n = s.model_concurrency
    embedder = limited(build_embedder(s, client, bedrock), n)
    reranker_raw = build_reranker(s, client, bedrock)
    return Providers(
        embedder=embedder,
        reranker=limited(reranker_raw, n) if reranker_raw is not None else None,
        answer=limited(build_llm(s, "answer", client, bedrock), n),
        judge=limited(build_llm(s, "judge", client, bedrock), n),
        budget=budget,
        client=client,
        bedrock=bedrock,
        bedrock_budget=bedrock_budget,
    )
