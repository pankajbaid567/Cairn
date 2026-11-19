"""Workers AI client contract tests through ``httpx.MockTransport``.

Requirements 14.1 to 14.7, 14.9, 14.11, 15.1 and 15.8. No test opens a socket or calls Cloudflare.

Fixtures are in ``data/workers_ai/``; see ``test_workers_ai_parse.py`` for their provenance.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn_core.config import WorkersAiSettings
from cairn_retrieval.cloudflare.budget import InMemoryUsageStore, NeuronBudget
from cairn_retrieval.cloudflare.client import (
    DAILY_LIMIT_CODES,
    RetryPolicy,
    WorkersAiClient,
    WorkersAiError,
    backoff_delay,
    classify,
    parse_retry_after,
)
from cairn_retrieval.cloudflare.parse import (
    MalformedResult,
    parse_embeddings,
    parse_rerank,
    parse_text,
)

DATA = Path(__file__).parent / "data" / "workers_ai"
ACCOUNT = "0123456789abcdef0123456789abcdef"
TOKEN = "cf-test-token-SECRET-0001"  # fake by construction
DAY = date(2026, 11, 3)
EMBED = "@cf/baai/bge-small-en-v1.5"
RERANK = "@cf/baai/bge-reranker-base"
GEMMA = "@cf/google/gemma-4-26b-a4b-it"
LLAMA = "@cf/meta/llama-3.1-8b-instruct-fp8-fast"
PAYLOAD_TEXT = "PAYLOAD-TEXT-quarterly-numbers"
PAYLOAD = {"text": [PAYLOAD_TEXT, "second"]}
ESTIMATE = 5


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in (
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "CAIRN_WORKERS_AI_BASE_URL",
        "CAIRN_WORKERS_AI_MAX_ATTEMPTS",
        "CAIRN_WORKERS_AI_CONNECT_TIMEOUT_S",
        "CAIRN_WORKERS_AI_READ_TIMEOUT_S",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def fixture_response(
    name: str, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status,
        content=(DATA / f"{name}.json").read_bytes(),
        headers={"content-type": "application/json", **(headers or {})},
    )


Step = httpx.Response | Exception


@dataclass
class Harness:
    """A scripted transport, the sleeps the client asked for, and the budget behind it."""

    steps: list[Step]
    requests: list[httpx.Request] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def settings(**overrides: Any) -> WorkersAiSettings:
    values: dict[str, Any] = {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "CLOUDFLARE_API_TOKEN": TOKEN}
    values.update(overrides)
    return WorkersAiSettings(_env_file=None, **values)


Factory = Callable[..., tuple[WorkersAiClient, Harness, NeuronBudget]]


@pytest.fixture
async def make_client() -> AsyncIterator[Factory]:
    clients: list[WorkersAiClient] = []

    def factory(
        steps: Sequence[Step],
        *,
        cap: int = 1000,
        policy: RetryPolicy | None = None,
        u: float = 0.5,
        cfg: WorkersAiSettings | None = None,
    ) -> tuple[WorkersAiClient, Harness, NeuronBudget]:
        harness = Harness(list(steps))
        budget = NeuronBudget(InMemoryUsageStore(), cap, today=lambda: DAY)
        client = WorkersAiClient(
            cfg or settings(),
            budget,
            transport=httpx.MockTransport(harness.handler),
            sleep=harness.sleep,
            uniform=lambda: u,
            policy=policy,
        )
        clients.append(client)
        return client, harness, budget

    yield factory
    for client in clients:
        await client.aclose()


def never_called(request: httpx.Request) -> httpx.Response:  # pragma: no cover - failure path
    raise AssertionError("the transport must not be called")


# --- request shape (Requirements 14.1, 14.3) ---------------------------------------------------


async def test_a_call_posts_the_payload_with_a_bearer_token(make_client: Factory) -> None:
    client, harness, _ = make_client([fixture_response("embeddings")])
    out = await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert out.attempts == 1
    (request,) = harness.requests
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.host == "api.cloudflare.com"
    assert request.url.path == f"/client/v4/accounts/{ACCOUNT}/ai/run/{EMBED}"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == PAYLOAD  # the inputs directly, no wrapper


async def test_the_base_url_is_configurable(make_client: Factory) -> None:
    cfg = settings(CAIRN_WORKERS_AI_BASE_URL="https://example.invalid/client/v4/")
    client, harness, _ = make_client([fixture_response("embeddings")], cfg=cfg)
    await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert harness.requests[0].url.host == "example.invalid"
    assert harness.requests[0].url.path == f"/client/v4/accounts/{ACCOUNT}/ai/run/{EMBED}"


async def test_timeouts_come_from_the_settings_and_the_call_can_override_the_read_timeout(
    make_client: Factory,
) -> None:
    cfg = settings(CAIRN_WORKERS_AI_CONNECT_TIMEOUT_S="2", CAIRN_WORKERS_AI_READ_TIMEOUT_S="11")
    client, harness, _ = make_client([fixture_response("embeddings")], cfg=cfg)
    await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE, read_timeout_s=60)
    first, second = (r.extensions["timeout"] for r in harness.requests)
    assert (first["connect"], first["read"]) == (2.0, 11.0)
    assert (second["connect"], second["read"]) == (2.0, 60.0)


def test_the_default_timeouts_are_5_and_30_seconds() -> None:
    cfg = settings()
    assert (cfg.connect_timeout_s, cfg.read_timeout_s, cfg.max_attempts) == (5.0, 30.0, 3)


@pytest.mark.parametrize(
    "model", ["", "bge", "@cf/x", "@cf/a/b/c", "@cf/a/../b", "@CF/a/b", "@cf/a/b?x=1"]
)
async def test_an_invalid_model_id_is_refused_without_a_call(
    make_client: Factory, model: str
) -> None:
    client, harness, budget = make_client([fixture_response("embeddings")])
    with pytest.raises(WorkersAiError) as exc:
        await client.run(model, PAYLOAD, estimate_neurons=ESTIMATE)
    assert exc.value.code == "bad_request"
    assert harness.requests == []
    assert await budget.used() == 0


# --- result shapes of the four default models and the legacy shape ------------------------------


async def test_embedding_result_parses(make_client: Factory) -> None:
    client, _, _ = make_client([fixture_response("embeddings")])
    out = await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert len(parse_embeddings(out.result, expected=2, dim=4)) == 2


async def test_rerank_result_parses(make_client: Factory) -> None:
    client, harness, _ = make_client([fixture_response("rerank")])
    body = {"query": "q", "contexts": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}
    out = await client.run(RERANK, body, estimate_neurons=ESTIMATE)
    assert parse_rerank(out.result, 3) == [-1.25, 0.75, 4.5]
    assert json.loads(harness.requests[0].content) == body


@pytest.mark.parametrize(
    ("name", "model"), [("text_openai", GEMMA), ("text_response_legacy", LLAMA)]
)
async def test_text_result_parses(make_client: Factory, name: str, model: str) -> None:
    client, _, _ = make_client([fixture_response(name)])
    out = await client.run(model, {"messages": []}, estimate_neurons=ESTIMATE)
    assert parse_text(out.result).text == "The Kestrel gateway restarts nightly."


# --- errors and retries (Requirements 14.4, 14.5, 14.6) -----------------------------------------


@pytest.mark.parametrize(
    ("name", "status", "code", "cf_codes"),
    [
        ("error_401", 401, "auth", (10000,)),
        ("error_403_paid_model", 403, "auth", (5035,)),
        ("error_400", 400, "bad_request", (5004,)),
        ("error_429_daily_limit", 429, "daily_limit", (3036,)),
    ],
)
async def test_errors_that_are_not_retryable_fail_on_the_first_attempt(
    make_client: Factory, name: str, status: int, code: str, cf_codes: tuple[int, ...]
) -> None:
    client, harness, budget = make_client([fixture_response(name, status)])
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    err = exc.value
    assert (err.code, err.status, err.attempts, err.cloudflare_codes) == (code, status, 1, cf_codes)
    assert len(harness.requests) == 1
    assert harness.sleeps == []
    assert await budget.used() == ESTIMATE  # reservations are not released


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("error_429_capacity", 429, "rate_limited"),
        ("error_500", 500, "server"),
        ("error_503", 503, "server"),
        ("error_503", 502, "server"),
        ("error_503", 504, "server"),
    ],
)
async def test_retryable_statuses_are_tried_3_times_in_total_then_fail(
    make_client: Factory, name: str, status: int, code: str
) -> None:
    client, harness, budget = make_client([fixture_response(name, status)])
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert (exc.value.code, exc.value.status, exc.value.attempts) == (code, status, 3)
    assert len(harness.requests) == 3
    assert len(harness.sleeps) == 2  # no wait after the last attempt
    assert await budget.used() == 3 * ESTIMATE  # each attempt reserved first


async def test_a_retry_that_succeeds_returns_the_result(make_client: Factory) -> None:
    client, harness, _ = make_client(
        [fixture_response("error_503", 503), fixture_response("embeddings")]
    )
    out = await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert out.attempts == 2
    assert len(harness.requests) == 2
    assert out.latency_ms >= 0


async def test_max_attempts_comes_from_the_settings(make_client: Factory) -> None:
    cfg = settings(CAIRN_WORKERS_AI_MAX_ATTEMPTS="2")
    client, harness, _ = make_client([fixture_response("error_500", 500)], cfg=cfg)
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert exc.value.attempts == 2
    assert len(harness.requests) == 2


async def test_a_408_is_a_timeout_error_that_is_not_retried(make_client: Factory) -> None:
    client, harness, _ = make_client([fixture_response("error_500", 408)])
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert (exc.value.code, exc.value.attempts) == ("timeout", 1)
    assert len(harness.requests) == 1


async def test_the_policy_can_narrow_the_retried_statuses(make_client: Factory) -> None:
    policy = RetryPolicy(retry_statuses=frozenset({503}))
    client, harness, _ = make_client([fixture_response("error_500", 500)], policy=policy)
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert exc.value.attempts == 1
    assert len(harness.requests) == 1


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (httpx.ConnectTimeout("connect timed out"), "connect"),
        (httpx.ReadTimeout("read timed out"), "read"),
    ],
)
async def test_timeouts_are_retried_then_reported_as_timeout(
    make_client: Factory, failure: Exception, kind: str
) -> None:
    client, harness, _ = make_client([failure])
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert (exc.value.code, exc.value.status, exc.value.attempts) == ("timeout", None, 3)
    assert len(harness.requests) == 3, kind


async def test_a_transport_failure_is_not_retried_and_hides_its_message(
    make_client: Factory,
) -> None:
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}"
    client, harness, _ = make_client([httpx.ConnectError(f"refused {url} {TOKEN}")])
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    err = exc.value
    assert (err.code, err.attempts, len(harness.requests)) == ("transport", 1, 1)
    assert err.__cause__ is None
    assert err.__suppress_context__ or err.__context__ is None
    assert ACCOUNT not in str(err) + repr(err)
    assert TOKEN not in str(err) + repr(err)


async def test_a_timeout_then_success_recovers(make_client: Factory) -> None:
    client, _, _ = make_client([httpx.ReadTimeout("slow"), fixture_response("embeddings")])
    out = await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert out.attempts == 2


# --- waits (Requirement 14.5) --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected_first_wait"),
    [("7", 7.0), ("0", 0.0), ("2.5", 2.5), ("120", 30.0)],
)
async def test_retry_after_is_waited_and_capped_at_30_seconds(
    make_client: Factory, header: str, expected_first_wait: float
) -> None:
    client, harness, _ = make_client(
        [
            fixture_response("error_429_capacity", 429, {"Retry-After": header}),
            fixture_response("embeddings"),
        ]
    )
    await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert harness.sleeps == [expected_first_wait]


@pytest.mark.parametrize("header", [None, "", "-3", "soon", "Wed, 21 Oct 2026 07:28:00 GMT", "nan"])
async def test_without_a_usable_retry_after_the_wait_is_jittered_backoff(
    make_client: Factory, header: str | None
) -> None:
    headers = {} if header is None else {"Retry-After": header}
    client, harness, _ = make_client([fixture_response("error_429_capacity", 429, headers)], u=0.5)
    with pytest.raises(WorkersAiError):
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    # u * min(8, 0.5 * 2 ** (attempt - 1)) for attempts 1 and 2
    assert harness.sleeps == [0.5 * 0.5, 0.5 * 1.0]


async def test_retry_after_on_a_server_error_is_also_used(make_client: Factory) -> None:
    client, harness, _ = make_client(
        [fixture_response("error_503", 503, {"Retry-After": "3"}), fixture_response("embeddings")]
    )
    await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert harness.sleeps == [3.0]


# --- not configured and budget (Requirements 14.2, 15.1, 15.8) -----------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"CLOUDFLARE_API_TOKEN": None}, {"CLOUDFLARE_ACCOUNT_ID": None}],
)
async def test_missing_credentials_raise_not_configured_without_a_call(
    overrides: dict[str, None],
) -> None:
    budget = NeuronBudget(InMemoryUsageStore(), 1000, today=lambda: DAY)
    values: dict[str, Any] = {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "CLOUDFLARE_API_TOKEN": TOKEN}
    values.update(overrides)
    cfg = WorkersAiSettings(_env_file=None, **{k: v for k, v in values.items() if v is not None})
    async with WorkersAiClient(cfg, budget, transport=httpx.MockTransport(never_called)) as client:
        with pytest.raises(WorkersAiError) as exc:
            await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert (exc.value.code, exc.value.attempts, exc.value.status) == ("not_configured", 0, None)
    assert await budget.used() == 0


async def test_a_refused_reservation_is_budget_exhausted_without_a_call() -> None:
    budget = NeuronBudget(InMemoryUsageStore(), 4, today=lambda: DAY)
    async with WorkersAiClient(
        settings(), budget, transport=httpx.MockTransport(never_called)
    ) as client:
        with pytest.raises(WorkersAiError) as exc:
            await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert (exc.value.code, exc.value.attempts) == ("budget_exhausted", 0)
    assert await budget.used() == 0  # the refused reservation changed nothing


async def test_every_retry_reserves_again_and_stops_when_the_cap_is_reached(
    make_client: Factory,
) -> None:
    # Room for two attempts only: the third reservation is refused before the third request.
    client, harness, budget = make_client(
        [fixture_response("error_500", 500)], cap=2 * ESTIMATE + 1
    )
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert (exc.value.code, exc.value.attempts) == ("budget_exhausted", 2)
    assert len(harness.requests) == 2
    assert await budget.used() == 2 * ESTIMATE


# --- bad responses (Requirement 14.9) ------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, content=b""),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"success": False, "result": {}, "errors": []}),
        httpx.Response(200, json={"success": True, "result": None}),
        httpx.Response(200, json={"success": True, "result": [1]}),
    ],
)
async def test_a_2xx_without_a_usable_envelope_is_bad_response_and_not_retried(
    make_client: Factory, response: httpx.Response
) -> None:
    client, harness, _ = make_client([response])
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert (exc.value.code, exc.value.status, exc.value.attempts) == ("bad_response", 200, 1)
    assert len(harness.requests) == 1


async def test_a_malformed_result_passes_the_envelope_and_is_caught_by_the_parser(
    make_client: Factory,
) -> None:
    client, _, _ = make_client([fixture_response("malformed_embeddings")])
    out = await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    with pytest.raises(MalformedResult):
        parse_embeddings(out.result, expected=2, dim=4)


async def test_an_error_status_with_a_body_that_is_not_json_still_classifies(
    make_client: Factory,
) -> None:
    client, _, _ = make_client([httpx.Response(401, content=b"Forbidden")])
    with pytest.raises(WorkersAiError) as exc:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    assert (exc.value.code, exc.value.cloudflare_codes) == ("auth", ())


# --- secret hygiene (Requirement 14.7) -----------------------------------------------------------


def _record_text(record: logging.LogRecord) -> str:
    return " ".join([record.getMessage(), *(str(v) for v in record.__dict__.values())])


@pytest.mark.parametrize(
    "steps",
    [
        [fixture_response("embeddings")],
        [fixture_response("error_500", 500)],
        [fixture_response("error_401", 401)],
        [httpx.ReadTimeout("slow")],
        [httpx.ConnectError("refused")],
    ],
)
async def test_nothing_secret_reaches_logs_exceptions_or_reprs(
    make_client: Factory, caplog: pytest.LogCaptureFixture, steps: list[Step]
) -> None:
    caplog.set_level(logging.DEBUG)
    client, _, _ = make_client(steps)
    error: WorkersAiError | None = None
    try:
        await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    except WorkersAiError as exc:
        error = exc
    secrets = (TOKEN, ACCOUNT, PAYLOAD_TEXT, "api.cloudflare.com", "Bearer")
    for record in caplog.records:
        text = _record_text(record)
        for secret in secrets:
            assert secret not in text, (record.name, secret)
    assert any(r.getMessage() == "workers_ai.call" for r in caplog.records)
    texts = [repr(client), repr(settings().api_token)]
    if error is not None:
        texts += [str(error), repr(error), *map(str, error.args)]
    for text in texts:
        for secret in secrets:
            assert secret not in text


async def test_the_call_log_line_has_counts_and_timing_only(
    make_client: Factory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cairn_retrieval")
    client, _, _ = make_client([fixture_response("error_503", 503), fixture_response("embeddings")])
    await client.run(EMBED, PAYLOAD, estimate_neurons=ESTIMATE)
    (record,) = [r for r in caplog.records if r.getMessage() == "workers_ai.call"]
    assert record.__dict__["model"] == EMBED
    assert (record.__dict__["attempts"], record.__dict__["status"]) == (2, 200)
    assert record.__dict__["neurons_reserved"] == 2 * ESTIMATE
    assert record.__dict__["latency_ms"] >= 0


async def test_the_httpx_and_httpcore_loggers_are_at_warning_or_above(make_client: Factory) -> None:
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    make_client([fixture_response("embeddings")])
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


async def test_the_http_client_has_no_default_headers_or_hooks(make_client: Factory) -> None:
    client, _, _ = make_client([fixture_response("embeddings")])
    http = client._http
    assert "authorization" not in {k.lower() for k in http.headers}
    assert http.event_hooks == {"request": [], "response": []}
    assert http.follow_redirects is False


# --- pure rules ----------------------------------------------------------------------------------


def test_daily_limit_codes_are_the_documented_free_allocation_code_only() -> None:
    assert frozenset({3036}) == DAILY_LIMIT_CODES
    assert 3040 not in DAILY_LIMIT_CODES  # capacity: a retryable 429


@pytest.mark.parametrize(
    ("outcome", "codes", "expected"),
    [
        (200, (), (None, False)),
        (204, (), (None, False)),
        (429, (), ("rate_limited", True)),
        (429, (3040,), ("rate_limited", True)),
        (429, (3036,), ("daily_limit", False)),
        (429, (3040, 3036), ("daily_limit", False)),
        (500, (), ("server", True)),
        (502, (), ("server", True)),
        (503, (), ("server", True)),
        (504, (), ("server", True)),
        (501, (), ("server", False)),
        (505, (), ("server", False)),
        (401, (), ("auth", False)),
        (403, (5035,), ("auth", False)),
        (400, (), ("bad_request", False)),
        (404, (3042,), ("bad_request", False)),
        (413, (3006,), ("bad_request", False)),
        (408, (3007,), ("timeout", False)),
        (302, (), ("bad_response", False)),
        ("connect_timeout", (), ("timeout", True)),
        ("read_timeout", (), ("timeout", True)),
        ("transport", (), ("transport", False)),
    ],
)
def test_classify(outcome: Any, codes: tuple[int, ...], expected: tuple[str | None, bool]) -> None:
    assert classify(outcome, codes) == expected


def test_backoff_delay_rules() -> None:
    policy = RetryPolicy()
    assert backoff_delay(1, policy, None, 0.0) == 0.0
    assert backoff_delay(1, policy, None, 0.5) == 0.25
    assert backoff_delay(2, policy, None, 0.5) == 0.5
    assert backoff_delay(5, policy, None, 0.5) == 4.0  # 0.5 * 2 ** 4 = 8, capped at 8, times 0.5
    assert backoff_delay(1000, policy, None, 0.999) == pytest.approx(0.999 * 8.0)
    assert backoff_delay(1, policy, 4.0, 0.9) == 4.0  # Retry-After wins
    assert backoff_delay(1, policy, 99.0, 0.1) == 30.0  # and is capped
    assert backoff_delay(1, policy, float("nan"), 0.5) == 0.25  # unusable: backoff
    assert backoff_delay(1, policy, -1.0, 0.5) == 0.25


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0.0),
        ("5", 5.0),
        (" 12 ", 12.0),
        ("1.5", 1.5),
        (None, None),
        ("", None),
        ("-1", None),
        ("1e3", None),
        ("abc", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected
