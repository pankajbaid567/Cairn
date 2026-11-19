"""Pure parsers for Workers AI responses (design §11, FT-21; Requirements 14.8 and 14.9).

Shapes come from FV8 (``docs/evidence/free-tier/verify/FV8.md``):

- Envelope: ``{"result": {...}, "success": true, "errors": [], "messages": []}``.
- Embeddings (``bge-small-en-v1.5``): ``result = {"shape": [n, d], "data": [[...], ...]}``.
- Rerank (``bge-reranker-base``): ``result = {"response": [{"id": i, "score": s}, ...]}``. The
  docs do not say whether every context comes back or in what order, so the parser sorts by id
  and requires every id ``0..n-1`` exactly once.
- Text (``gemma-4-26b-a4b-it``, ``glm-4.7-flash``): the OpenAI chat shape
  ``{"choices": [{"message": {"content": str}, "finish_reason": ...}], "usage": {...}}``. The
  older ``{"response": str, "usage": {...}}`` shape is accepted as a fallback.

A result that does not have its expected shape, or that holds a non-finite number, raises
``MalformedResult``. The functions never return a partial value. The adapters turn
``MalformedResult`` into ``WorkersAiError("bad_response")``. Messages name what is wrong, never
a value from the response (a response can echo user text).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeGuard

__all__ = [
    "MalformedResult",
    "TextResult",
    "parse_embeddings",
    "parse_envelope",
    "parse_rerank",
    "parse_text",
]


class MalformedResult(ValueError):
    """The response does not have its expected shape. The message holds no response content."""


@dataclass(frozen=True)
class TextResult:
    """A text completion: the text, its token counts and why it stopped.

    ``completion_tokens`` includes reasoning tokens for the reasoning models (FV9);
    ``reasoning_tokens`` is the part reported separately, 0 when the response has no detail.
    ``finish_reason`` is ``"length"`` when the output bound was reached: an answer provider
    should treat empty text with that reason as a failure (FV9).
    """

    text: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int = 0
    finish_reason: str | None = None


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:  # an integer too large for a float
        return False


def _non_negative_int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MalformedResult(f"{what} is not a non-negative integer")
    return value


def parse_envelope(body: Any) -> tuple[bool, Mapping[str, Any] | None, tuple[int, ...]]:
    """Split a response body into ``(success, result, cloudflare error codes)``.

    ``success`` is true only when the body is an object with ``"success": true`` and an object
    ``"result"``. Codes are the integer ``code`` of each entry of ``errors``; anything else in
    ``errors`` is ignored. Never raises: an unusable body gives ``(False, None, ())``.
    """
    if not isinstance(body, Mapping):
        return False, None, ()
    codes: list[int] = []
    errors = body.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, str | bytes):
        for entry in errors:
            if isinstance(entry, Mapping):
                code = entry.get("code")
                if _is_int(code):
                    codes.append(int(code))
    result = body.get("result")
    if body.get("success") is True and isinstance(result, Mapping):
        return True, result, tuple(codes)
    return False, None, tuple(codes)


def parse_embeddings(result: Mapping[str, Any], expected: int, dim: int) -> list[list[float]]:
    """``{"shape": [n, d], "data": [[...], ...]}`` to ``n`` vectors of length ``d``.

    Rejects ``n != expected``, ``d != dim``, a ``shape`` that disagrees with ``data``, and any
    value that is not a finite number.
    """
    if not isinstance(result, Mapping):
        raise MalformedResult("embedding result is not an object")
    shape = result.get("shape")
    data = result.get("data")
    if (
        not isinstance(shape, Sequence)
        or isinstance(shape, str | bytes)
        or len(shape) != 2
        or not all(_is_int(x) for x in shape)
    ):
        raise MalformedResult("embedding shape is not [n, d]")
    n, d = int(shape[0]), int(shape[1])
    if n != expected:
        raise MalformedResult("embedding count differs from the number of inputs")
    if d != dim:
        raise MalformedResult("embedding dimension differs from the expected dimension")
    if not isinstance(data, Sequence) or isinstance(data, str | bytes) or len(data) != n:
        raise MalformedResult("embedding data does not hold one vector per input")
    vectors: list[list[float]] = []
    for row in data:
        if not isinstance(row, Sequence) or isinstance(row, str | bytes) or len(row) != d:
            raise MalformedResult("an embedding vector has the wrong length")
        if not all(_is_finite_number(x) for x in row):
            raise MalformedResult("an embedding vector holds a value that is not a finite number")
        vectors.append([float(x) for x in row])
    return vectors


def parse_rerank(result: Mapping[str, Any], n: int) -> list[float]:
    """``{"response": [{"id": i, "score": s}, ...]}`` to ``n`` scores in input order.

    Every id ``0..n-1`` must appear exactly once and every score must be a finite number.
    """
    if not isinstance(result, Mapping):
        raise MalformedResult("rerank result is not an object")
    entries = result.get("response")
    if not isinstance(entries, Sequence) or isinstance(entries, str | bytes):
        raise MalformedResult("rerank response is not a list")
    if len(entries) != n:
        raise MalformedResult("rerank response does not score every passage once")
    scores: dict[int, float] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise MalformedResult("a rerank entry is not an object")
        ident, score = entry.get("id"), entry.get("score")
        if not _is_int(ident) or not 0 <= int(ident) < n:
            raise MalformedResult("a rerank entry has an id outside the passages")
        if not _is_finite_number(score):
            raise MalformedResult("a rerank score is not a finite number")
        if int(ident) in scores:
            raise MalformedResult("a rerank id appears twice")
        scores[int(ident)] = float(score)
    return [scores[i] for i in range(n)]


def parse_text(result: Mapping[str, Any]) -> TextResult:
    """Text and token counts from the OpenAI-style or the ``response`` shape (FV8).

    ``choices[0].message.content`` is used when ``choices`` is present; ``content`` must be a
    string (``null`` means the reasoning used the whole bound, FV8) and ``usage`` must carry
    non-negative integer ``prompt_tokens`` and ``completion_tokens``.
    """
    if not isinstance(result, Mapping):
        raise MalformedResult("text result is not an object")
    finish_reason: str | None = None
    if "choices" in result:
        choices = result["choices"]
        if not isinstance(choices, Sequence) or isinstance(choices, str | bytes) or not choices:
            raise MalformedResult("text choices is not a non-empty list")
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        if not isinstance(message, Mapping):
            raise MalformedResult("the first choice has no message")
        text = message.get("content")
        reason = first.get("finish_reason")
        finish_reason = reason if isinstance(reason, str) else None
    else:
        text = result.get("response")
    if not isinstance(text, str):
        raise MalformedResult("the text is missing or not a string")
    usage = result.get("usage")
    if not isinstance(usage, Mapping):
        raise MalformedResult("usage is missing")
    prompt = _non_negative_int(usage.get("prompt_tokens"), "prompt_tokens")
    completion = _non_negative_int(usage.get("completion_tokens"), "completion_tokens")
    reasoning = 0
    details = usage.get("completion_tokens_details")
    if isinstance(details, Mapping) and details.get("reasoning_tokens") is not None:
        reasoning = _non_negative_int(details["reasoning_tokens"], "reasoning_tokens")
    return TextResult(text, prompt, completion, reasoning, finish_reason)
