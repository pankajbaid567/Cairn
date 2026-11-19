"""Workers AI response parsers against the contract fixtures (Requirements 14.8 and 14.9).

The fixtures in ``data/workers_ai/`` follow the documented envelopes recorded in FV8. They are
written from the documentation, not recorded from a live call; error codes other than 3036, 3040,
5004, 5035 and 10000 are illustrative.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from cairn_retrieval.cloudflare.parse import (
    MalformedResult,
    TextResult,
    parse_embeddings,
    parse_envelope,
    parse_rerank,
    parse_text,
)

DATA = Path(__file__).parent / "data" / "workers_ai"


def load(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    return body


def result_of(name: str) -> dict[str, Any]:
    ok, result, _ = parse_envelope(load(name))
    assert ok
    assert result is not None
    return dict(result)


# --- envelope --------------------------------------------------------------------------------


def test_envelope_of_a_success_body() -> None:
    ok, result, codes = parse_envelope(load("embeddings"))
    assert ok
    assert result is not None
    assert result["shape"] == [2, 4]
    assert codes == ()


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("error_401", 10000),
        ("error_400", 5004),
        ("error_403_paid_model", 5035),
        ("error_429_capacity", 3040),
        ("error_429_daily_limit", 3036),
    ],
)
def test_envelope_of_an_error_body_carries_its_code(name: str, code: int) -> None:
    assert parse_envelope(load(name)) == (False, None, (code,))


@pytest.mark.parametrize(
    "body",
    [None, [], "x", 3, {}, {"success": True}, {"success": True, "result": []}, {"result": {}}],
)
def test_envelope_of_an_unusable_body_is_a_failure_and_never_raises(body: Any) -> None:
    assert parse_envelope(body)[:2] == (False, None)


def test_envelope_ignores_entries_without_an_integer_code() -> None:
    body = {"success": False, "errors": [{"code": "x"}, {"code": True}, "y", {"code": 7}]}
    assert parse_envelope(body) == (False, None, (7,))


# --- embeddings ------------------------------------------------------------------------------


def test_embeddings_fixture_gives_one_vector_per_input() -> None:
    vectors = parse_embeddings(result_of("embeddings"), expected=2, dim=4)
    assert vectors == [[0.25, -0.5, 0.125, 0.0625], [0.5, 0.5, -0.25, 1.0]]


@pytest.mark.parametrize(("expected", "dim"), [(1, 4), (3, 4), (2, 3), (2, 384)])
def test_embeddings_reject_a_count_or_dimension_that_differs(expected: int, dim: int) -> None:
    with pytest.raises(MalformedResult):
        parse_embeddings(result_of("embeddings"), expected=expected, dim=dim)


def test_embeddings_reject_data_that_disagrees_with_shape() -> None:
    with pytest.raises(MalformedResult):
        parse_embeddings(result_of("malformed_embeddings"), expected=2, dim=4)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "1.0", None, True, 10**400])
def test_embeddings_reject_a_value_that_is_not_a_finite_number(bad: Any) -> None:
    result = {"shape": [1, 2], "data": [[0.5, bad]]}
    with pytest.raises(MalformedResult):
        parse_embeddings(result, expected=1, dim=2)


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"shape": [1, 2]},
        {"data": [[0.1, 0.2]]},
        {"shape": "12", "data": [[0.1, 0.2]]},
        {"shape": [1, 2, 3], "data": [[0.1, 0.2]]},
        {"shape": [1.0, 2], "data": [[0.1, 0.2]]},
        {"shape": [1, 2], "data": "ab"},
        {"shape": [1, 2], "data": [["a", "b"]]},
    ],
)
def test_embeddings_reject_other_shapes(result: dict[str, Any]) -> None:
    with pytest.raises(MalformedResult):
        parse_embeddings(result, expected=1, dim=2)


def test_malformed_messages_hold_no_response_content() -> None:
    result = {"shape": [1, 2], "data": [[0.5, "SECRET-TEXT"]]}
    with pytest.raises(MalformedResult) as exc:
        parse_embeddings(result, expected=1, dim=2)
    assert "SECRET-TEXT" not in str(exc.value)


# --- rerank ----------------------------------------------------------------------------------


def test_rerank_returns_scores_in_input_order_whatever_the_response_order() -> None:
    assert parse_rerank(result_of("rerank"), 3) == [-1.25, 0.75, 4.5]


@pytest.mark.parametrize(
    "response",
    [
        [{"id": 0, "score": 1.0}],  # a passage is missing
        [{"id": 0, "score": 1.0}, {"id": 0, "score": 2.0}, {"id": 1, "score": 3.0}],  # repeated
        [{"id": 0, "score": 1.0}, {"id": 1, "score": 2.0}, {"id": 3, "score": 3.0}],  # outside
        [{"id": -1, "score": 1.0}, {"id": 1, "score": 2.0}, {"id": 2, "score": 3.0}],
        [{"id": 0, "score": math.nan}, {"id": 1, "score": 2.0}, {"id": 2, "score": 3.0}],
        [{"id": 0, "score": "1"}, {"id": 1, "score": 2.0}, {"id": 2, "score": 3.0}],
        [{"id": 0}, {"id": 1, "score": 2.0}, {"id": 2, "score": 3.0}],
        [{"id": "0", "score": 1.0}, {"id": 1, "score": 2.0}, {"id": 2, "score": 3.0}],
        [1, 2, 3],
        "abc",
    ],
)
def test_rerank_rejects_a_response_that_does_not_score_each_passage_once(response: Any) -> None:
    with pytest.raises(MalformedResult):
        parse_rerank({"response": response}, 3)


def test_rerank_rejects_a_result_without_response() -> None:
    with pytest.raises(MalformedResult):
        parse_rerank({}, 3)


# --- text ------------------------------------------------------------------------------------


def test_text_in_the_openai_shape() -> None:
    assert parse_text(result_of("text_openai")) == TextResult(
        text="The Kestrel gateway restarts nightly.",
        prompt_tokens=120,
        completion_tokens=48,
        reasoning_tokens=30,
        finish_reason="stop",
    )


def test_text_in_the_legacy_response_shape() -> None:
    assert parse_text(result_of("text_response_legacy")) == TextResult(
        text="The Kestrel gateway restarts nightly.",
        prompt_tokens=64,
        completion_tokens=12,
    )


def test_text_with_null_content_is_rejected() -> None:
    # Reasoning used the whole output bound (finish_reason "length"): a failure, not an answer.
    with pytest.raises(MalformedResult):
        parse_text(result_of("text_openai_length"))


def test_text_with_empty_content_keeps_its_finish_reason() -> None:
    result = result_of("text_openai")
    result["choices"][0]["message"]["content"] = ""
    result["choices"][0]["finish_reason"] = "length"
    parsed = parse_text(result)
    assert (parsed.text, parsed.finish_reason) == ("", "length")


@pytest.mark.parametrize(
    "usage", [None, {}, {"prompt_tokens": 1}, {"prompt_tokens": 1, "completion_tokens": -1}]
)
def test_text_rejects_missing_or_invalid_usage(usage: Any) -> None:
    result = result_of("text_response_legacy")
    result["usage"] = usage
    with pytest.raises(MalformedResult):
        parse_text(result)


@pytest.mark.parametrize("count", [1.5, True, "3", None, math.nan])
def test_text_rejects_token_counts_that_are_not_integers(count: Any) -> None:
    result = result_of("text_response_legacy")
    result["usage"]["completion_tokens"] = count
    with pytest.raises(MalformedResult):
        parse_text(result)


@pytest.mark.parametrize("choices", [[], "x", [{}], [{"message": "x"}], [{"message": {}}]])
def test_text_rejects_bad_choices(choices: Any) -> None:
    result = result_of("text_openai")
    result["choices"] = choices
    with pytest.raises(MalformedResult):
        parse_text(result)


def test_text_rejects_a_result_with_neither_shape() -> None:
    with pytest.raises(MalformedResult):
        parse_text({"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
