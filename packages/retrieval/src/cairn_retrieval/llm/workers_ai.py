"""Workers AI text model adapter (design §13, FT-23; Requirements 16.1 and 16.2).

The default answer and judge models (``gemma-4-26b-a4b-it``, ``glm-4.7-flash``) are reasoning
models (FV9). Their reasoning tokens count as output tokens, so:

- the output bound is sent as ``max_completion_tokens`` (``max_tokens`` is deprecated for them,
  FV8) and the same number is the output term of the neuron estimate;
- a call can spend the whole bound on reasoning and return no answer: ``content`` is null or
  empty with ``finish_reason`` ``length``. That is a failure (``bad_response``), never an empty
  answer, so a judge cannot grade nothing and an answer provider cannot cite nothing.

Only the models with the OpenAI-style request body are tested (FV8); another model that needs
``max_tokens`` would need its own body.
"""

from __future__ import annotations

from typing import Final

from cairn_retrieval.cloudflare.adapt import bad_response
from cairn_retrieval.cloudflare.budget import estimate_neurons, estimate_tokens
from cairn_retrieval.cloudflare.client import WorkersAiClient
from cairn_retrieval.cloudflare.parse import MalformedResult, parse_text
from cairn_retrieval.llm.types import LlmRequest, LlmResponse

__all__ = ["LLM_READ_TIMEOUT_S", "WorkersAiLlm"]

LLM_READ_TIMEOUT_S: Final = 60.0
"""Read timeout of a text call. Generation (reasoning included) takes longer than an embedding;
the client default of 30 s would cut it off and the retry would pay for it twice."""


class WorkersAiLlm:
    """Completes one request on a Workers AI text model."""

    provider = "workers_ai"

    def __init__(
        self, client: WorkersAiClient, model: str, *, read_timeout_s: float = LLM_READ_TIMEOUT_S
    ) -> None:
        self._client = client
        self._read_timeout_s = read_timeout_s
        self.model = model

    async def complete(self, req: LlmRequest) -> LlmResponse:
        payload = {
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "max_completion_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        tokens_in = estimate_tokens(req.system) + estimate_tokens(req.user)
        neurons = estimate_neurons(self.model, tokens_in, req.max_tokens)
        run = await self._client.run(
            self.model, payload, estimate_neurons=neurons, read_timeout_s=self._read_timeout_s
        )
        try:
            parsed = parse_text(run.result)
        except MalformedResult:  # includes null content (reasoning used the whole bound)
            raise bad_response(self.model, run.attempts) from None
        if parsed.finish_reason == "length" and not parsed.text.strip():
            raise bad_response(self.model, run.attempts)
        return LlmResponse(
            text=parsed.text,
            tokens_in=parsed.prompt_tokens,
            tokens_out=parsed.completion_tokens,
            latency_ms=run.latency_ms,
            provider=self.provider,
            model=self.model,
            finish_reason=parsed.finish_reason,
        )
