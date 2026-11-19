"""Helpers the provider adapters share (design §13)."""

from __future__ import annotations

from cairn_retrieval.cloudflare.client import WorkersAiError

__all__ = ["bad_response"]


def bad_response(model: str, attempts: int) -> WorkersAiError:
    """The call returned HTTP 200 but its result is unusable (wrong shape, or an empty answer
    cut off at the output bound). The message holds no response content."""
    return WorkersAiError("bad_response", model=model, status=200, attempts=attempts)
