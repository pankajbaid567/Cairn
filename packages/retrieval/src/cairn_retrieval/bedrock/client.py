"""Async Bedrock client with bounded retries and a daily dollar cap (mirrors ``WorkersAiClient``).

The boto3 calls are blocking, so each runs in a worker thread. Failures become
``WorkersAiError`` (the one provider error the pipeline and workflow already degrade on), whose
text is only ``<code> model=<model> status=<status> attempts=<n>``: no prompt, no response.
Retried: throttling, 5xx-like service errors and timeouts, with jittered exponential backoff.
Not retried: access denied, validation errors, an unpriced model, an exhausted budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from cairn_core.config import BedrockSettings
from cairn_retrieval.bedrock.pricing import UnpricedBedrockModel
from cairn_retrieval.cloudflare.budget import BudgetExhausted, NeuronBudget
from cairn_retrieval.cloudflare.client import ErrorCode, WorkersAiError

__all__ = ["BedrockClient", "ConverseResult", "EmbedResult"]

log = logging.getLogger(__name__)

_RETRYABLE: Final = {
    "ThrottlingException": "rate_limited",
    "TooManyRequestsException": "rate_limited",
    "ServiceUnavailableException": "server",
    "InternalServerException": "server",
    "ModelTimeoutException": "timeout",
    "ModelNotReadyException": "server",
}
_NOT_RETRYABLE: Final = {
    "AccessDeniedException": "auth",
    "UnrecognizedClientException": "auth",
    "ExpiredTokenException": "auth",
    "ValidationException": "bad_request",
    "ResourceNotFoundException": "bad_request",
    "ModelErrorException": "server",
}


@dataclass(frozen=True)
class ConverseResult:
    text: str
    tokens_in: int
    tokens_out: int
    stop_reason: str | None
    latency_ms: float = field(compare=False)
    attempts: int = 1


@dataclass(frozen=True)
class EmbedResult:
    vector: list[float]
    tokens: int
    latency_ms: float = field(compare=False)
    attempts: int = 1


def _classify(exc: BaseException) -> tuple[ErrorCode, bool, int | None]:
    """``(code, retryable, http status)`` of a boto3 failure, without importing botocore."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = str(error.get("Code", ""))
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        status = int(status) if isinstance(status, int) else None
        if code in _RETRYABLE:
            return _RETRYABLE[code], True, status  # type: ignore[return-value]
        if code in _NOT_RETRYABLE:
            return _NOT_RETRYABLE[code], False, status  # type: ignore[return-value]
        return "server", False, status
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout", True, None
    return "transport", True, None


class BedrockClient:
    """One shared client for every Bedrock call the process makes."""

    def __init__(
        self,
        settings: BedrockSettings,
        budget: NeuronBudget | None = None,
        *,
        runtime: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._s = settings
        self._budget = budget
        self._runtime = runtime
        self._sleep = sleep
        self._jitter = jitter

    def __repr__(self) -> str:
        return f"BedrockClient(region={self._s.bedrock_region!r})"

    def _client(self) -> Any:
        if self._runtime is None:
            import boto3  # imported lazily: only a process that uses Bedrock needs it
            from botocore.config import Config

            self._runtime = boto3.client(
                "bedrock-runtime",
                region_name=self._s.bedrock_region,
                config=Config(
                    connect_timeout=self._s.bedrock_connect_timeout_s,
                    read_timeout=self._s.bedrock_read_timeout_s,
                    retries={"max_attempts": 1},
                ),
            )
        return self._runtime

    async def aclose(self) -> None:
        return None

    async def _call(
        self, model: str, estimate_micro_usd: int, fn: Callable[[Any], Any]
    ) -> tuple[Any, int, float]:
        """Run ``fn(runtime)`` with reservation and retries. Returns ``(result, attempts, ms)``."""
        failure: WorkersAiError | None = None
        attempts = 0
        started = time.monotonic()
        for attempts in range(1, self._s.bedrock_max_attempts + 1):
            if self._budget is not None:
                try:
                    await self._budget.reserve(estimate_micro_usd)
                except BudgetExhausted:
                    raise WorkersAiError(
                        "budget_exhausted", model=model, attempts=attempts - 1
                    ) from None
            try:
                result = await asyncio.to_thread(fn, self._client())
            except UnpricedBedrockModel:
                raise
            except Exception as exc:  # boto3 raises many types; all are classified below
                code, retryable, status = _classify(exc)
                failure = WorkersAiError(code, model=model, status=status, attempts=attempts)
                log.warning("bedrock.call_failed", extra={"model": model, "code": code})
                if not retryable or attempts >= self._s.bedrock_max_attempts:
                    raise failure from None
                delay = min(8.0, 0.5 * 2 ** (attempts - 1)) * (0.5 + self._jitter())
                await self._sleep(delay)
                continue
            return result, attempts, (time.monotonic() - started) * 1000.0
        raise failure or WorkersAiError("server", model=model, attempts=attempts)

    async def converse(
        self,
        model: str,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        estimate_micro_usd: int,
        reasoning_effort: str = "",
    ) -> ConverseResult:
        args: dict[str, Any] = {
            "modelId": model,
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        # Only the gpt-oss family takes a reasoning effort; another model would reject the field.
        if reasoning_effort and model.startswith("openai.gpt-oss"):
            args["additionalModelRequestFields"] = {"reasoning_effort": reasoning_effort}
        res, attempts, ms = await self._call(
            model, estimate_micro_usd, lambda rt: rt.converse(**args)
        )
        try:
            blocks = res["output"]["message"]["content"]
            text = "".join(b["text"] for b in blocks if "text" in b)
            usage = res.get("usage") or {}
            return ConverseResult(
                text=text,
                tokens_in=int(usage.get("inputTokens", 0)),
                tokens_out=int(usage.get("outputTokens", 0)),
                stop_reason=res.get("stopReason"),
                latency_ms=ms,
                attempts=attempts,
            )
        except (KeyError, TypeError, ValueError):
            raise WorkersAiError(
                "bad_response", model=model, status=200, attempts=attempts
            ) from None

    async def embed(
        self, model: str, text: str, *, dim: int, estimate_micro_usd: int
    ) -> EmbedResult:
        body = json.dumps({"inputText": text, "dimensions": dim, "normalize": True})

        def run(rt: Any) -> Any:
            return rt.invoke_model(
                modelId=model, body=body, contentType="application/json", accept="application/json"
            )

        res, attempts, ms = await self._call(model, estimate_micro_usd, run)
        try:
            payload = json.loads(res["body"].read())
            vector = [float(x) for x in payload["embedding"]]
            if len(vector) != dim:
                raise ValueError("wrong embedding length")
            return EmbedResult(vector, int(payload.get("inputTextTokenCount", 0)), ms, attempts)
        except (KeyError, TypeError, ValueError):
            raise WorkersAiError(
                "bad_response", model=model, status=200, attempts=attempts
            ) from None
