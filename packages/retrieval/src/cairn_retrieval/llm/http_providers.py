"""Optional Anthropic and OpenAI answer models over plain ``httpx`` (PD1-13; no vendor SDK).

They exist so the model comparison (1.14) is a configuration change. Keys come from the
environment as ``SecretStr`` and are used per request; nothing is logged but codes and counts.
"""

from __future__ import annotations

import time
from typing import Any, Final

import httpx
from pydantic import SecretStr

from cairn_retrieval.cloudflare.adapt import bad_response  # noqa: F401  (shared error shape)
from cairn_retrieval.cloudflare.client import WorkersAiError
from cairn_retrieval.llm.types import LlmRequest, LlmResponse

TIMEOUT_S: Final = 60.0


def _failure(provider: str, model: str, status: int | None, code: str) -> WorkersAiError:
    # Same exception type as Workers AI so the pipeline handles every provider alike. The code
    # is one of the fixed ErrorCode values; the provider and model name travel in `model`.
    return WorkersAiError(code, model=f"{provider}:{model}", status=status, attempts=1)  # type: ignore[arg-type]


class AnthropicLlm:
    provider = "anthropic"

    def __init__(
        self, api_key: SecretStr, model: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._key = api_key
        self.model = model
        self._client = client

    async def complete(self, req: LlmRequest) -> LlmResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
        }
        headers = {
            "x-api-key": self._key.get_secret_value(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        started = time.monotonic()
        try:
            async with self._http() as http:
                r = await http.post(
                    "https://api.anthropic.com/v1/messages", json=body, headers=headers
                )
        except httpx.TimeoutException:
            raise _failure(self.provider, self.model, None, "timeout") from None
        except httpx.HTTPError:
            raise _failure(self.provider, self.model, None, "transport") from None
        if r.status_code == 429:
            raise _failure(self.provider, self.model, 429, "rate_limited")
        if r.status_code in (401, 403):
            raise _failure(self.provider, self.model, r.status_code, "auth")
        if r.status_code >= 500:
            raise _failure(self.provider, self.model, r.status_code, "server")
        if r.status_code != 200:
            raise _failure(self.provider, self.model, r.status_code, "bad_request")
        try:
            data = r.json()
            text = "".join(p.get("text", "") for p in data["content"] if p.get("type") == "text")
            usage = data.get("usage", {})
            tokens_in, tokens_out = (
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
            )
        except (ValueError, KeyError, TypeError):
            raise _failure(self.provider, self.model, 200, "bad_response") from None
        if not text.strip():
            raise _failure(self.provider, self.model, 200, "bad_response")
        return LlmResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=(time.monotonic() - started) * 1000,
            provider=self.provider,
            model=self.model,
            finish_reason=data.get("stop_reason"),
        )

    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=TIMEOUT_S)


class OpenAiLlm:
    provider = "openai"

    def __init__(
        self, api_key: SecretStr, model: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._key = api_key
        self.model = model
        self._client = client

    async def complete(self, req: LlmRequest) -> LlmResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "max_completion_tokens": req.max_tokens,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
        }
        headers = {
            "authorization": f"Bearer {self._key.get_secret_value()}",
            "content-type": "application/json",
        }
        started = time.monotonic()
        try:
            async with self._client or httpx.AsyncClient(timeout=TIMEOUT_S) as http:
                r = await http.post(
                    "https://api.openai.com/v1/chat/completions", json=body, headers=headers
                )
        except httpx.TimeoutException:
            raise _failure(self.provider, self.model, None, "timeout") from None
        except httpx.HTTPError:
            raise _failure(self.provider, self.model, None, "transport") from None
        if r.status_code == 429:
            raise _failure(self.provider, self.model, 429, "rate_limited")
        if r.status_code in (401, 403):
            raise _failure(self.provider, self.model, r.status_code, "auth")
        if r.status_code >= 500:
            raise _failure(self.provider, self.model, r.status_code, "server")
        if r.status_code != 200:
            raise _failure(self.provider, self.model, r.status_code, "bad_request")
        try:
            data = r.json()
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            usage = data.get("usage", {})
            tokens_in, tokens_out = (
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
            )
        except (ValueError, KeyError, TypeError, IndexError):
            raise _failure(self.provider, self.model, 200, "bad_response") from None
        if not text.strip():
            raise _failure(self.provider, self.model, 200, "bad_response")
        return LlmResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=(time.monotonic() - started) * 1000,
            provider=self.provider,
            model=self.model,
            finish_reason=choice.get("finish_reason"),
        )
