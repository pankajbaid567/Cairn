"""Property test for the Workers AI client's secret hygiene (free-tier retarget, Requirement 14.7).

**Validates: Requirements 14.7**

For any API token, account id and payload text, and for a scripted run of the client through a
successful call or any failure path (every error status with and without Cloudflare codes, a
``Retry-After`` header, connect and read timeouts, transport failures, a 2xx without a usable
envelope, retries exhausted, budget exhausted, not configured), nothing the test can observe
contains the token, the request URL, the host, the ``Bearer`` scheme word or the payload text:

- no log record captured by a handler attached to the root logger at DEBUG (message, every
  ``extra`` field, and any rendered exception text);
- no exception in the raised error's cause and context chain (``str``, ``repr`` and ``args``);
- the ``repr`` of the client.

The scripted server is hostile on purpose: error bodies, response headers and transport
exception messages all echo the URL, the token, the bearer header and the payload text, so a
client that copied any of them into a log line or an error would be caught.

The async code runs through ``asyncio.run`` inside a synchronous test because Hypothesis does
not reset function-scoped fixtures between examples; the test attaches its own logging handler
instead of using ``caplog``, and clears the Cloudflare environment keys inside each example.
"""
# Feature: cairn-free-tier-retarget, Property 16: The client never exposes the token, URL or payload

from __future__ import annotations

import asyncio
import logging
import os
import string
import traceback
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any
from unittest import mock

import httpx
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_core.config import WorkersAiSettings
from cairn_retrieval.cloudflare.budget import InMemoryUsageStore, NeuronBudget
from cairn_retrieval.cloudflare.client import WorkersAiClient, WorkersAiError

BASE_URL = "https://api.cloudflare.com/client/v4"
HOST = "api.cloudflare.com"
DAY = date(2026, 11, 3)
MODELS = [
    "@cf/baai/bge-small-en-v1.5",
    "@cf/baai/bge-reranker-base",
    "@cf/google/gemma-4-26b-a4b-it",
    "@cf/meta/llama-3.1-8b-instruct-fp8-fast",
]
_ENVIRONMENT_KEYS = (
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "CAIRN_WORKERS_AI_BASE_URL",
    "CAIRN_WORKERS_AI_MAX_ATTEMPTS",
    "CAIRN_WORKERS_AI_CONNECT_TIMEOUT_S",
    "CAIRN_WORKERS_AI_READ_TIMEOUT_S",
)
_SUCCESS_BODY = {"success": True, "errors": [], "messages": [], "result": {"data": [[0.25]]}}


# --- strategies ---------------------------------------------------------------------------------

# Distinct, recognizable prefixes keep a drawn secret from matching ordinary log text by chance.
_token_chars = string.ascii_letters + string.digits + "-_"
tokens = st.text(alphabet=_token_chars, min_size=12, max_size=48).map(lambda s: f"cftok-{s}")
account_ids = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)
payload_texts = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=4, max_size=40
).map(lambda s: f"PAYLOAD-{s}")


@dataclass(frozen=True)
class Step:
    """One scripted transport outcome: a response (``status``) or an exception (``exc``)."""

    status: int | None = None
    cf_codes: tuple[int, ...] = ()
    retry_after: str | None = None
    raw: str | None = None  # a non-envelope 2xx body kind, or ``None``
    exc: str | None = None


_error_statuses = st.sampled_from([302, 400, 401, 403, 404, 408, 413, 429, 500, 501, 502, 503, 504])
_cf_codes = st.lists(st.sampled_from([3036, 3040, 5004, 5035, 10000]), max_size=2).map(tuple)
_retry_after = st.sampled_from(
    [None, "0", "3", "2.5", "120", "soon", "-1", "", "Wed, 21 Oct 2026 07:28:00 GMT"]
)
_exceptions = st.sampled_from(
    [
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "connect_error",
        "read_error",
        "protocol_error",
        "too_many_redirects",
    ]
)
steps = st.one_of(
    st.just(Step(status=200)),
    st.builds(Step, status=_error_statuses, cf_codes=_cf_codes, retry_after=_retry_after),
    st.builds(Step, status=st.just(200), raw=st.sampled_from(["html", "empty", "list", "failed"])),
    st.builds(Step, exc=_exceptions),
)

Mode = str  # "normal", "budget", "no_token", "no_account"
modes = st.sampled_from(["normal", "normal", "normal", "budget", "no_token", "no_account"])


# --- the scripted run ---------------------------------------------------------------------------


class Capture(logging.Handler):
    """Collects every record and the text a formatter could print for it."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.texts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        parts = [record.name, record.getMessage(), *(repr(v) for v in record.__dict__.values())]
        if record.exc_info is not None:
            parts += traceback.format_exception(*record.exc_info)
        if record.stack_info:
            parts.append(record.stack_info)
        self.texts.append(" ".join(parts))


def _chain(error: BaseException) -> Iterator[BaseException]:
    """The error and everything reachable through its cause and (unsuppressed) context."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None


def _make_exception(kind: str, message: str) -> Exception:
    return {
        "connect_timeout": httpx.ConnectTimeout,
        "read_timeout": httpx.ReadTimeout,
        "write_timeout": httpx.WriteTimeout,
        "pool_timeout": httpx.PoolTimeout,
        "connect_error": httpx.ConnectError,
        "read_error": httpx.ReadError,
        "protocol_error": httpx.RemoteProtocolError,
        "too_many_redirects": httpx.TooManyRedirects,
    }[kind](message)


async def _scenario(
    *,
    token: str,
    account: str,
    payload_text: str,
    model: str,
    script: list[Step],
    mode: Mode,
    max_attempts: int,
    estimate: int,
    cap_extra: int,
    u: float,
) -> None:
    url = f"{BASE_URL}/accounts/{account}/ai/run/{model}"
    echo = f"{url} {HOST} {token} Bearer {token} {payload_text}"
    secrets = (token, account, payload_text, url, HOST, "Bearer")
    remaining = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        step = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if step.exc is not None:
            raise _make_exception(step.exc, f"failed {echo}")
        # Header values must be ASCII for httpx; the URL, host and token already are.
        headers = {"x-echo": echo.encode("ascii", "replace").decode("ascii")}
        if step.retry_after is not None:
            headers["retry-after"] = step.retry_after
        if step.status == 200:
            if step.raw is None:
                return httpx.Response(200, json=_SUCCESS_BODY, headers=headers)
            bodies: dict[str, dict[str, Any]] = {
                "html": {"content": f"<html>{echo}</html>".encode()},
                "empty": {"content": b""},
                "list": {"json": [echo]},
                "failed": {
                    "json": {"success": False, "result": None, "errors": [{"message": echo}]}
                },
            }
            return httpx.Response(200, headers=headers, **bodies[step.raw])
        assert step.status is not None
        errors = [{"code": c, "message": echo} for c in step.cf_codes] or [{"message": echo}]
        return httpx.Response(
            step.status,
            json={"success": False, "errors": errors, "messages": [echo], "result": None},
            headers={**headers, "location": headers["x-echo"]},
        )

    values: dict[str, Any] = {"CAIRN_WORKERS_AI_MAX_ATTEMPTS": max_attempts}
    if mode != "no_account":
        values["CLOUDFLARE_ACCOUNT_ID"] = account
    if mode != "no_token":
        values["CLOUDFLARE_API_TOKEN"] = token
    cfg = WorkersAiSettings(_env_file=None, **values)

    cap = cap_extra if mode == "budget" else 10**9
    budget = NeuronBudget(InMemoryUsageStore(), cap, today=lambda: DAY)

    async def sleep(seconds: float) -> None:  # never really wait
        assert seconds >= 0

    capture = Capture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    error: WorkersAiError | None = None
    try:
        async with WorkersAiClient(
            cfg,
            budget,
            transport=httpx.MockTransport(handler),
            sleep=sleep,
            uniform=lambda: u,
        ) as client:
            client_repr = repr(client)
            try:
                await client.run(
                    model, {"text": [payload_text, "second"]}, estimate_neurons=estimate
                )
            except WorkersAiError as exc:
                error = exc
            client_repr_after = repr(client)
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)

    # The scenario reached the paths it was meant to reach.
    if mode in ("no_token", "no_account"):
        assert error is not None
        assert (error.code, error.attempts) == ("not_configured", 0)
    elif mode == "budget" and cap_extra < estimate:
        assert error is not None
        assert (error.code, error.attempts) == ("budget_exhausted", 0)
    logged_calls = [r for r in capture.records if r.getMessage() == "workers_ai.call"]
    assert len(logged_calls) == (0 if mode in ("no_token", "no_account") else 1)

    # Nothing observable carries a secret.
    observed: list[str] = [*capture.texts, client_repr, client_repr_after]
    if error is not None:
        for link in _chain(error):
            observed += [str(link), repr(link), *map(repr, link.args)]
    for text in observed:
        for secret in secrets:
            assert secret not in text, ("secret leaked", secret in (token, account), text)


@given(
    token=tokens,
    account=account_ids,
    payload_text=payload_texts,
    model=st.sampled_from(MODELS),
    script=st.lists(steps, min_size=1, max_size=6),
    mode=modes,
    max_attempts=st.integers(min_value=1, max_value=5),
    estimate=st.integers(min_value=1, max_value=50),
    cap_extra=st.integers(min_value=0, max_value=150),
    u=st.floats(min_value=0.0, max_value=0.999),
)
@settings(deadline=None)
def test_the_client_never_exposes_the_token_url_or_payload(
    token: str,
    account: str,
    payload_text: str,
    model: str,
    script: list[Step],
    mode: Mode,
    max_attempts: int,
    estimate: int,
    cap_extra: int,
    u: float,
) -> None:
    with mock.patch.dict(os.environ):
        for key in _ENVIRONMENT_KEYS:
            os.environ.pop(key, None)
        asyncio.run(
            _scenario(
                token=token,
                account=account,
                payload_text=payload_text,
                model=model,
                script=script,
                mode=mode,
                max_attempts=max_attempts,
                estimate=estimate,
                cap_extra=cap_extra,
                u=u,
            )
        )
