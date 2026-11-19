"""Property test for malformed Workers AI results (task 8.12).

Feature: cairn-free-tier-retarget, Property 18: Malformed results are rejected

**Validates: Requirements 14.9**

Start from a valid result built the way the Property 17 generators build one (1 to 50 embedding
rows of finite floats, a rerank score list with its ids in shuffled order, a text result in the
OpenAI-style or the legacy ``response`` shape), apply exactly one mutation, and the matching
parser raises ``MalformedResult`` and returns nothing:

- embeddings: a wrong row count (in ``shape``, in ``data``, or against the expected count), a
  wrong dimension (in ``shape``, in one row, or against the expected dimension), and a NaN or an
  infinity in any cell;
- rerank: a missing id, an id out of range or of the wrong type, a duplicate id, an extra entry,
  a non-finite or missing score, and a count that differs from the passages;
- text: a missing, null or non-string text field, no choices, and a missing or invalid ``usage``;
- the envelope: ``success: false`` gives no result at all, so no parser ever sees a value.

Results go through ``parse_envelope`` first, as they do in the client. Every parametrized case
below is one mutation kind, so each kind is exercised on every run.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cairn_retrieval.cloudflare.parse import (
    MalformedResult,
    parse_embeddings,
    parse_envelope,
    parse_rerank,
    parse_text,
)

PBT = settings(
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    deadline=None,
)

finite = st.floats(allow_nan=False, allow_infinity=False)
non_finite = st.sampled_from([math.nan, math.inf, -math.inf])


def envelope(result: dict[str, Any]) -> dict[str, Any]:
    """The documented success envelope around ``result``."""
    return {"result": result, "success": True, "errors": [], "messages": []}


def unwrap(result: dict[str, Any]) -> dict[str, Any]:
    """Send ``result`` through ``parse_envelope`` like the client does; it must arrive intact."""
    ok, unwrapped, _ = parse_envelope(envelope(result))
    assert ok
    assert unwrapped is not None
    return dict(unwrapped)


# --- embeddings ------------------------------------------------------------------------------

EMBEDDING_KINDS = [
    "expected_count_differs",
    "expected_dim_differs",
    "shape_count_differs",
    "shape_dim_differs",
    "data_row_missing",
    "data_row_extra",
    "row_too_short",
    "row_too_long",
    "non_finite_cell",
]


@st.composite
def malformed_embeddings(draw: st.DrawFn, kind: str) -> tuple[dict[str, Any], int, int]:
    """A valid embedding result with one mutation: ``(result, expected, dim)``."""
    n = draw(st.integers(min_value=1, max_value=50))
    d = draw(st.integers(min_value=1, max_value=12))
    data = [draw(st.lists(finite, min_size=d, max_size=d)) for _ in range(n)]
    shape = [n, d]
    expected, dim = n, d
    row = draw(st.integers(min_value=0, max_value=n - 1))
    col = draw(st.integers(min_value=0, max_value=d - 1))
    if kind == "expected_count_differs":
        expected = draw(st.integers(min_value=0, max_value=60).filter(lambda x: x != n))
    elif kind == "expected_dim_differs":
        dim = draw(st.integers(min_value=0, max_value=20).filter(lambda x: x != d))
    elif kind == "shape_count_differs":
        shape[0] = draw(st.integers(min_value=0, max_value=60).filter(lambda x: x != n))
    elif kind == "shape_dim_differs":
        shape[1] = draw(st.integers(min_value=0, max_value=20).filter(lambda x: x != d))
    elif kind == "data_row_missing":
        del data[row]
    elif kind == "data_row_extra":
        data.append(list(data[row]))
    elif kind == "row_too_short":
        del data[row][col]
    elif kind == "row_too_long":
        data[row].append(draw(finite))
    elif kind == "non_finite_cell":
        data[row][col] = draw(non_finite)
    else:  # pragma: no cover
        raise AssertionError(kind)
    return {"shape": shape, "data": data}, expected, dim


@pytest.mark.parametrize("kind", EMBEDDING_KINDS)
@PBT
@given(data=st.data())
def test_a_malformed_embedding_result_is_rejected(kind: str, data: st.DataObject) -> None:
    result, expected, dim = data.draw(malformed_embeddings(kind))
    with pytest.raises(MalformedResult):
        parse_embeddings(unwrap(result), expected=expected, dim=dim)


# --- rerank ----------------------------------------------------------------------------------

RERANK_KINDS = [
    "entry_missing",
    "entry_extra",
    "id_duplicated",
    "id_out_of_range",
    "id_wrong_type",
    "score_non_finite",
    "score_missing",
    "score_wrong_type",
    "count_differs",
]


@st.composite
def malformed_rerank(draw: st.DrawFn, kind: str) -> tuple[dict[str, Any], int]:
    """A valid rerank result (ids in shuffled order) with one mutation: ``(result, n)``."""
    n = draw(st.integers(min_value=2 if kind == "id_duplicated" else 1, max_value=50))
    scores = draw(st.lists(finite, min_size=n, max_size=n))
    order = draw(st.permutations(range(n)))
    entries: list[Any] = [{"id": i, "score": scores[i]} for i in order]
    at = draw(st.integers(min_value=0, max_value=n - 1))
    asked = n
    if kind == "entry_missing":
        del entries[at]
    elif kind == "entry_extra":
        entries.append({"id": draw(st.integers(min_value=0, max_value=n - 1)), "score": 0.0})
    elif kind == "id_duplicated":
        other = draw(st.integers(min_value=0, max_value=n - 1).filter(lambda x: x != at))
        entries[at]["id"] = entries[other]["id"]
    elif kind == "id_out_of_range":
        entries[at]["id"] = draw(
            st.one_of(
                st.integers(min_value=n, max_value=10**9),
                st.integers(min_value=-(10**9), max_value=-1),
            )
        )
    elif kind == "id_wrong_type":
        entries[at]["id"] = draw(
            st.sampled_from([float(entries[at]["id"]), str(entries[at]["id"]), None, True])
        )
    elif kind == "score_non_finite":
        entries[at]["score"] = draw(non_finite)
    elif kind == "score_missing":
        del entries[at]["score"]
    elif kind == "score_wrong_type":
        entries[at]["score"] = draw(st.sampled_from(["1.0", None, True, [1.0]]))
    elif kind == "count_differs":
        asked = draw(st.integers(min_value=0, max_value=60).filter(lambda x: x != n))
    else:  # pragma: no cover
        raise AssertionError(kind)
    return {"response": entries}, asked


@pytest.mark.parametrize("kind", RERANK_KINDS)
@PBT
@given(data=st.data())
def test_a_malformed_rerank_result_is_rejected(kind: str, data: st.DataObject) -> None:
    result, n = data.draw(malformed_rerank(kind))
    with pytest.raises(MalformedResult):
        parse_rerank(unwrap(result), n)


@PBT
@given(data=st.data())
def test_a_rerank_result_without_response_is_rejected(data: st.DataObject) -> None:
    n = data.draw(st.integers(min_value=1, max_value=50))
    extra = data.draw(st.dictionaries(st.sampled_from(["scores", "data", "id"]), st.integers()))
    with pytest.raises(MalformedResult):
        parse_rerank(unwrap(extra), n)


# --- text ------------------------------------------------------------------------------------

counts = st.integers(min_value=0, max_value=10**7)


@st.composite
def valid_text_result(draw: st.DrawFn, shape: str) -> dict[str, Any]:
    """A valid text result in the ``"openai"`` or the ``"legacy"`` shape."""
    usage: dict[str, Any] = {
        "prompt_tokens": draw(counts),
        "completion_tokens": draw(counts),
    }
    if draw(st.booleans()):
        usage["completion_tokens_details"] = {"reasoning_tokens": draw(counts)}
    text = draw(st.text())
    if shape == "legacy":
        return {"response": text, "usage": usage}
    reason = draw(st.sampled_from(["stop", "length", None]))
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": reason}],
        "usage": usage,
    }


def _drop_content(result: dict[str, Any]) -> None:
    if "choices" in result:
        del result["choices"][0]["message"]["content"]
    else:
        del result["response"]


def _set_content(value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(result: dict[str, Any]) -> None:
        if "choices" in result:
            result["choices"][0]["message"]["content"] = value
        else:
            result["response"] = value

    return mutate


def _set_usage(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(result: dict[str, Any]) -> None:
        result["usage"][key] = value

    return mutate


def _drop_usage_key(key: str) -> Callable[[dict[str, Any]], None]:
    def mutate(result: dict[str, Any]) -> None:
        del result["usage"][key]

    return mutate


def _set_reasoning(value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(result: dict[str, Any]) -> None:
        result["usage"]["completion_tokens_details"] = {"reasoning_tokens": value}

    return mutate


def _set_top(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(result: dict[str, Any]) -> None:
        result[key] = value

    return mutate


def _drop_top(key: str) -> Callable[[dict[str, Any]], None]:
    def mutate(result: dict[str, Any]) -> None:
        del result[key]

    return mutate


def _drop_message(result: dict[str, Any]) -> None:
    del result["choices"][0]["message"]


BAD_TEXTS = [None, 7, 1.5, True, ["a"], {"text": "a"}]
BAD_COUNTS = [-1, 1.5, True, "3", None, math.nan, [3]]

# (name, shapes it applies to, mutation)
TEXT_MUTATIONS: list[tuple[str, tuple[str, ...], Callable[[dict[str, Any]], None]]] = [
    ("text_missing", ("openai", "legacy"), _drop_content),
    *[(f"text_bad_{i}", ("openai", "legacy"), _set_content(v)) for i, v in enumerate(BAD_TEXTS)],
    ("choices_empty", ("openai",), _set_top("choices", [])),
    ("choices_not_a_list", ("openai",), _set_top("choices", "x")),
    ("choice_not_an_object", ("openai",), _set_top("choices", [3])),
    ("message_missing", ("openai",), _drop_message),
    ("usage_missing", ("openai", "legacy"), _drop_top("usage")),
    ("usage_not_an_object", ("openai", "legacy"), _set_top("usage", [1, 2])),
    ("prompt_tokens_missing", ("openai", "legacy"), _drop_usage_key("prompt_tokens")),
    ("completion_tokens_missing", ("openai", "legacy"), _drop_usage_key("completion_tokens")),
    *[
        (f"prompt_tokens_bad_{i}", ("openai", "legacy"), _set_usage("prompt_tokens", v))
        for i, v in enumerate(BAD_COUNTS)
    ],
    *[
        (f"completion_tokens_bad_{i}", ("openai", "legacy"), _set_usage("completion_tokens", v))
        for i, v in enumerate(BAD_COUNTS)
    ],
    *[
        (f"reasoning_tokens_bad_{i}", ("openai", "legacy"), _set_reasoning(v))
        for i, v in enumerate(v for v in BAD_COUNTS if v is not None)
    ],
]

TEXT_CASES = [
    pytest.param(shape, mutate, id=f"{shape}-{name}")
    for name, shapes, mutate in TEXT_MUTATIONS
    for shape in shapes
]


@pytest.mark.parametrize(("shape", "mutate"), TEXT_CASES)
@PBT
@given(data=st.data())
def test_a_malformed_text_result_is_rejected(
    shape: str, mutate: Callable[[dict[str, Any]], None], data: st.DataObject
) -> None:
    result = copy.deepcopy(data.draw(valid_text_result(shape)))
    mutate(result)
    with pytest.raises(MalformedResult):
        parse_text(unwrap(result))


@pytest.mark.parametrize("shape", ["openai", "legacy"])
@PBT
@given(data=st.data())
def test_the_unmutated_text_result_is_accepted(shape: str, data: st.DataObject) -> None:
    """Control: the generator only produces rejections through its mutation."""
    result = data.draw(valid_text_result(shape))
    parsed = parse_text(unwrap(result))
    assert parsed.prompt_tokens == result["usage"]["prompt_tokens"]
    assert parsed.completion_tokens == result["usage"]["completion_tokens"]


# --- envelope with success: false ------------------------------------------------------------


@PBT
@given(
    data=st.data(),
    codes=st.lists(st.integers(min_value=1000, max_value=99999), max_size=3),
)
def test_a_success_false_envelope_yields_no_result(data: st.DataObject, codes: list[int]) -> None:
    n = data.draw(st.integers(min_value=1, max_value=50))
    d = data.draw(st.integers(min_value=1, max_value=12))
    vectors = [data.draw(st.lists(finite, min_size=d, max_size=d)) for _ in range(n)]
    body = envelope({"shape": [n, d], "data": vectors})
    body["success"] = False
    body["errors"] = [{"code": c, "message": "x"} for c in codes]
    # The embeddings inside are valid, yet the failed envelope hands nothing to a parser.
    assert parse_envelope(body) == (False, None, tuple(codes))
