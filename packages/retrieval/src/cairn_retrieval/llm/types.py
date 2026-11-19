"""Request, response and protocol of an answer or judge model (design §13; Requirement 16.1)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["LlmProvider", "LlmRequest", "LlmResponse"]


class LlmRequest(BaseModel):
    """One completion. ``max_tokens`` bounds the *output*, and for a reasoning model (gemma,
    glm) the reasoning tokens count as output (FV9), so it has to cover both."""

    model_config = ConfigDict(frozen=True)

    system: str
    user: str
    max_tokens: int = Field(ge=1)
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    evidence_ids: tuple[str, ...] = ()


class LlmResponse(BaseModel):
    """The text with the provider's token counts (reasoning tokens included in ``tokens_out``)."""

    model_config = ConfigDict(frozen=True)

    text: str
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    latency_ms: float = Field(ge=0.0)
    provider: str
    model: str
    finish_reason: str | None = None
    """Why generation stopped (``stop``, ``length``, ...); ``None`` when the provider has none.
    A non-empty text with ``length`` was cut off at ``max_tokens``."""


@runtime_checkable
class LlmProvider(Protocol):
    """A model that completes one request."""

    provider: str
    model: str

    async def complete(self, req: LlmRequest) -> LlmResponse:
        """Raises ``WorkersAiError`` (or the provider's error) when no usable answer comes back."""
        ...
