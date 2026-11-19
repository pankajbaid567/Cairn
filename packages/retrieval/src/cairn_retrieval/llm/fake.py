"""Deterministic LLM for tests, CI and local runs (design §13; Requirement 16.5)."""

from __future__ import annotations

import hashlib
from typing import Final

from cairn_retrieval.cloudflare.budget import estimate_tokens
from cairn_retrieval.llm.types import LlmRequest, LlmResponse

__all__ = ["FAKE_LLM_MODEL", "FAKE_LLM_PROVIDER", "FakeLlm"]

FAKE_LLM_PROVIDER: Final = "fake"
FAKE_LLM_MODEL: Final = "fake-llm-v1"


class FakeLlm:
    """Answers ``fake-answer:`` plus the first 16 hex digits of sha256(system + user).

    The same request always gives the same text and the same token counts (from
    ``estimate_tokens``); nothing leaves the process. ``evidence_ids`` and ``temperature`` do not
    change the answer.
    """

    provider = FAKE_LLM_PROVIDER
    model = FAKE_LLM_MODEL

    async def complete(self, req: LlmRequest) -> LlmResponse:
        digest = hashlib.sha256((req.system + req.user).encode("utf-8")).hexdigest()
        text = f"fake-answer:{digest[:16]}"
        return LlmResponse(
            text=text,
            tokens_in=estimate_tokens(req.system) + estimate_tokens(req.user),
            tokens_out=estimate_tokens(text),
            latency_ms=0.0,
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
        )
