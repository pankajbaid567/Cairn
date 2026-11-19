"""Workers AI REST client (design §11, FT-20 and FT-21; Requirement 14).

One call is ``POST {base_url}/accounts/{account_id}/ai/run/{model}`` with a bearer token and the
model's inputs as the JSON body (FV8, ``docs/evidence/free-tier/verify/FV8.md``). The client:

- reserves the attempt's neuron estimate from the ``NeuronBudget`` *before every attempt*; a
  refusal becomes ``WorkersAiError("budget_exhausted")`` with no network call (Requirement 15.8);
- raises ``not_configured`` without a network call when the account id or token is missing;
- applies a connect and a read timeout to each attempt and retries up to ``max_attempts`` in
  total on status 429 (other than the daily-limit error), 500, 502, 503, 504 and on connect or
  read timeouts;
- waits ``Retry-After`` (capped) when the response has it and a jittered exponential delay
  otherwise (FV8: whether a 429 carries it is undocumented, so both cases are handled);
- never puts the token, the URL, a header or the payload into a log record, an exception message
  or a ``repr``. ``str(WorkersAiError)`` is ``<code> model=<model> status=<status> attempts=<n>``.

Reservations are never released, so a failed attempt still counts against the day (the guard is
conservative on purpose, see ``budget``). A failure of the usage store itself (for example
Postgres unreachable) propagates unchanged: the guard fails closed and no call is made.

``classify`` and ``backoff_delay`` are pure so their rules can be tested without a transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Self

import httpx

from cairn_core.config import WorkersAiSettings
from cairn_retrieval.cloudflare.budget import BudgetExhausted, NeuronBudget
from cairn_retrieval.cloudflare.parse import parse_envelope

__all__ = [
    "DAILY_LIMIT_CODES",
    "ErrorCode",
    "Outcome",
    "RetryPolicy",
    "RunResult",
    "WorkersAiClient",
    "WorkersAiError",
    "backoff_delay",
    "classify",
    "parse_retry_after",
]

log = logging.getLogger(__name__)

ErrorCode = Literal[
    "auth",
    "bad_request",
    "rate_limited",
    "daily_limit",
    "server",
    "timeout",
    "transport",
    "bad_response",
    "budget_exhausted",
    "not_configured",
]

Outcome = int | Literal["connect_timeout", "read_timeout", "transport"]
"""What an attempt produced: an HTTP status, or the kind of failure before a response."""

DAILY_LIMIT_CODES: Final[frozenset[int]] = frozenset({3036})
"""Cloudflare error codes for "daily free allocation of 10,000 neurons used up" (FV8: 3036, HTTP
429). Code 3040 (capacity temporarily exceeded) is also a 429 but is retryable, not listed."""

MODEL_PATTERN: Final = re.compile(r"^@cf/[a-z0-9._-]+/[a-z0-9._-]+$")
_ACCOUNT_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")
_RETRYABLE_SERVER_STATUSES: Final = frozenset({500, 502, 503, 504})
_MAX_BACKOFF_EXPONENT: Final = 32


class WorkersAiError(Exception):
    """A Workers AI call failed. ``str()`` is ``<code> model=<model> status=<status> attempts=<n>``
    and nothing else: no URL, no header, no response body, no payload."""

    code: ErrorCode
    status: int | None
    model: str
    attempts: int
    cloudflare_codes: tuple[int, ...]

    def __init__(
        self,
        code: ErrorCode,
        *,
        model: str,
        status: int | None = None,
        attempts: int = 0,
        cloudflare_codes: Sequence[int] = (),
    ) -> None:
        super().__init__(f"{code} model={model} status={status} attempts={attempts}")
        self.code = code
        self.status = status
        self.model = model
        self.attempts = attempts
        self.cloudflare_codes = tuple(cloudflare_codes)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry bounds. ``max_attempts`` counts all attempts, so 3 means two retries."""

    max_attempts: int = 3
    base_s: float = 0.5
    cap_s: float = 8.0
    retry_after_cap_s: float = 30.0
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RunResult:
    """The envelope's ``result`` with how many attempts it took and the total latency."""

    result: Mapping[str, Any]
    attempts: int
    latency_ms: float = field(compare=False)


def classify(
    outcome: Outcome, cloudflare_codes: Sequence[int] = ()
) -> tuple[ErrorCode | None, bool]:
    """``(error code, retryable)`` for one attempt. ``(None, False)`` for a 2xx.

    - A Cloudflare code in ``DAILY_LIMIT_CODES`` on any non-2xx status is ``daily_limit``, never
      retried (Requirement 14.6).
    - 429 otherwise is ``rate_limited`` and retryable (this includes code 3040, FV8).
    - 500, 502, 503, 504 are ``server`` and retryable; other 5xx are ``server``, not retried.
    - 401 and 403 (including 5035, a paid-only model) are ``auth``.
    - 408 is ``timeout`` and not retried (Requirement 14.4 lists no 408); other 4xx are
      ``bad_request``; a 1xx or 3xx is ``bad_response``.
    - A connect or read timeout before any response is ``timeout`` and retryable; any other
      transport failure is ``transport`` and not retried.

    ``RetryPolicy.retry_statuses`` can narrow the retryable statuses further; the client applies it.
    """
    if outcome == "connect_timeout" or outcome == "read_timeout":
        return "timeout", True
    if outcome == "transport":
        return "transport", False
    status = int(outcome)
    if 200 <= status < 300:
        return None, False
    if any(code in DAILY_LIMIT_CODES for code in cloudflare_codes):
        return "daily_limit", False
    if status == 429:
        return "rate_limited", True
    if status in (401, 403):
        return "auth", False
    if status == 408:
        return "timeout", False
    if 400 <= status < 500:
        return "bad_request", False
    if status >= 500:
        return "server", status in _RETRYABLE_SERVER_STATUSES
    return "bad_response", False


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header given as a non-negative number; ``None`` for an
    absent, empty, negative, non-finite or HTTP-date value (the backoff then applies)."""
    if value is None:
        return None
    text = value.strip()
    if not text or not re.fullmatch(r"\d+(\.\d+)?", text):
        return None
    seconds = float(text)
    return seconds if math.isfinite(seconds) else None


def backoff_delay(
    attempt: int, policy: RetryPolicy, retry_after_s: float | None, u: float
) -> float:
    """Seconds to wait after failed attempt number ``attempt`` (1-based). ``u`` is in [0, 1).

    A usable ``Retry-After`` wins, bounded by ``retry_after_cap_s``. Otherwise the delay is
    ``u * min(cap_s, base_s * 2 ** (attempt - 1))``.
    """
    if retry_after_s is not None and math.isfinite(retry_after_s) and retry_after_s >= 0:
        return min(retry_after_s, policy.retry_after_cap_s)
    exponent = min(max(attempt - 1, 0), _MAX_BACKOFF_EXPONENT)
    return u * min(policy.cap_s, policy.base_s * 2.0**exponent)


def _error_codes(response: httpx.Response) -> tuple[int, ...]:
    """Cloudflare error codes from a response body; ``()`` when the body is not a JSON envelope."""
    try:
        body = json.loads(response.content)
    except ValueError:
        return ()
    return parse_envelope(body)[2]


class WorkersAiClient:
    """Calls Workers AI models; see the module docstring for the rules it applies."""

    def __init__(
        self,
        settings: WorkersAiSettings,
        budget: NeuronBudget,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[], float] = random.random,
        policy: RetryPolicy | None = None,
    ) -> None:
        self._account_id = settings.account_id
        self._token = settings.api_token
        self._base_url = str(settings.base_url).rstrip("/")
        self._connect_timeout_s = settings.connect_timeout_s
        self._read_timeout_s = settings.read_timeout_s
        self._budget = budget
        self._sleep = sleep
        self._uniform = uniform
        self._policy = policy or RetryPolicy(max_attempts=settings.max_attempts)
        # httpx logs "HTTP Request: POST <url>" at INFO; keep URLs out of logs (Requirement 14.7).
        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            if logger.level == logging.NOTSET or logger.level < logging.WARNING:
                logger.setLevel(logging.WARNING)
        # No default headers, no event hooks, no redirects: the bearer header is added per call.
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False)

    def __repr__(self) -> str:
        configured = self._account_id is not None and self._token is not None
        return f"WorkersAiClient(configured={configured})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def run(
        self,
        model: str,
        payload: Mapping[str, Any],
        *,
        estimate_neurons: int,
        read_timeout_s: float | None = None,
    ) -> RunResult:
        """Run ``model`` on ``payload`` and return the envelope's ``result``.

        ``estimate_neurons`` is reserved before each attempt. Raises ``WorkersAiError``.
        """
        if self._account_id is None or self._token is None:
            raise WorkersAiError("not_configured", model=model)
        if _ACCOUNT_PATTERN.fullmatch(self._account_id) is None:
            raise WorkersAiError("not_configured", model=model)
        if MODEL_PATTERN.fullmatch(model) is None:
            raise WorkersAiError("bad_request", model=model)
        url = f"{self._base_url}/accounts/{self._account_id}/ai/run/{model}"
        headers = {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        timeout = httpx.Timeout(
            connect=self._connect_timeout_s,
            read=read_timeout_s if read_timeout_s is not None else self._read_timeout_s,
            write=read_timeout_s if read_timeout_s is not None else self._read_timeout_s,
            pool=self._connect_timeout_s,
        )
        started = time.perf_counter()
        attempts = 0
        reserved = 0
        final_status: int | None = None
        failure: WorkersAiError | None = None
        result: Mapping[str, Any] | None = None
        try:
            while True:
                try:
                    await self._budget.reserve(estimate_neurons)
                except BudgetExhausted:
                    failure = WorkersAiError("budget_exhausted", model=model, attempts=attempts)
                    break
                reserved += estimate_neurons
                attempts += 1
                outcome, response = await self._attempt(url, headers, payload, timeout)
                codes: tuple[int, ...] = ()
                retry_after: float | None = None
                if response is not None:
                    final_status = response.status_code
                    if not response.is_success:
                        codes = _error_codes(response)
                        retry_after = parse_retry_after(response.headers.get("retry-after"))
                else:
                    final_status = None
                code, retryable = classify(outcome, codes)
                if code is None:
                    if response is not None:
                        result, failure = self._read_result(response, model, attempts)
                        break
                    code, retryable = "transport", False  # unreachable: a 2xx has a response
                status = outcome if isinstance(outcome, int) else None
                if isinstance(outcome, int):
                    retryable = retryable and outcome in self._policy.retry_statuses
                if not retryable or attempts >= self._policy.max_attempts:
                    failure = WorkersAiError(
                        code, model=model, status=status, attempts=attempts, cloudflare_codes=codes
                    )
                    break
                await self._sleep(
                    backoff_delay(attempts, self._policy, retry_after, self._uniform())
                )
        finally:
            self._log_call(
                model, attempts, final_status, started, reserved, failure.code if failure else None
            )
        if failure is not None:
            raise failure
        if result is None:  # unreachable: every exit from the loop sets a result or a failure
            raise WorkersAiError("bad_response", model=model, attempts=attempts)
        return RunResult(result, attempts, (time.perf_counter() - started) * 1000.0)

    async def _attempt(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        http_timeout: httpx.Timeout,
    ) -> tuple[Outcome, httpx.Response | None]:
        """One request. Returns the outcome and the response (``None`` before any response).

        httpx exceptions are reduced to a kind here, so no exception text (which can name the
        URL) is kept or chained.
        """
        try:
            response = await self._http.post(
                url, json=payload, headers=headers, timeout=http_timeout
            )
        except httpx.ConnectTimeout:
            return "connect_timeout", None
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            return "read_timeout", None
        except httpx.HTTPError:
            return "transport", None
        return response.status_code, response

    @staticmethod
    def _read_result(
        response: httpx.Response, model: str, attempts: int
    ) -> tuple[Mapping[str, Any] | None, WorkersAiError | None]:
        """Unwrap a 2xx envelope, or a ``bad_response`` error when it is not one."""
        try:
            body = json.loads(response.content)
        except ValueError:
            body = None
        success, result, codes = parse_envelope(body)
        if not success or result is None:
            error = WorkersAiError(
                "bad_response",
                model=model,
                status=response.status_code,
                attempts=attempts,
                cloudflare_codes=codes,
            )
            return None, error
        return result, None

    @staticmethod
    def _log_call(
        model: str,
        attempts: int,
        status: int | None,
        started: float,
        neurons: int,
        error: ErrorCode | None,
    ) -> None:
        """One line per call. Fields are the model id, counts and timing, never text or URLs."""
        log.log(
            logging.INFO if error is None else logging.WARNING,
            "workers_ai.call",
            extra={
                "model": model,
                "attempts": attempts,
                "status": status,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
                "neurons_reserved": neurons,
                "error": error,
            },
        )
