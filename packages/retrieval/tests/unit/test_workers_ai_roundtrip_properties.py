"""Property test for result round trips (task 8.11).

Feature: cairn-free-tier-retarget
Property 17: Workers AI results round-trip through their envelopes

**Validates: Requirements 14.8, 14.10**

For any embedding matrix (n from 1 to 50, the configured dimension, finite floats), any rerank
score list delivered with its ids in shuffled order, and any text result with token counts in
either documented shape, encoding the value in the documented Workers AI envelope and parsing it
returns an equal value: the same vectors in order, the scores in input order, and the same text
and counts.

Each case builds the envelope the way the FV8 fixtures do, serializes it to JSON text and reads
it back (so float formatting is part of the round trip), then runs ``parse_envelope`` followed by
the matching parser. The envelope must report success, no error codes, and hand back the result
object. No transport, socket or sleep is involved; the parsers are pure.

The default embedding model returns 384 dimensions. A full 50 by 384 matrix per example is slow
to generate, so the property runs over small dimensions with up to 50 rows, and over the
configured 384 with fewer rows.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from cairn_retrieval.cloudflare.parse import (
    TextResult,
    parse_embeddings,
    parse_envelope,
    parse_rerank,
    parse_text,
)

CONFIGURED_DIM = 384

finite = st.floats(allow_nan=False, allow_infinity=False)
counts = st.integers(min_value=0, max_value=10**9)
messages = st.lists(st.text(max_size=20), max_size=3)


def wrap(result: dict[str, Any]) -> Any:
    """The documented envelope around ``result``, sent as JSON text and read back."""
    envelope = {"result": result, "success": True, "errors": [], "messages": []}
    return json.loads(json.dumps(envelope))


def unwrap(body: Any) -> dict[str, Any]:
    ok, result, codes = parse_envelope(body)
    assert ok
    assert codes == ()
    assert result is not None
    return dict(result)


def matrices(dim: int, max_rows: int) -> st.SearchStrategy[list[list[float]]]:
    row = st.lists(finite, min_size=dim, max_size=dim)
    return st.lists(row, min_size=1, max_size=max_rows)


# --- embeddings ------------------------------------------------------------------------------


@given(
    dim=st.integers(min_value=1, max_value=8),
    data=st.data(),
)
def test_embeddings_round_trip_for_any_row_count(dim: int, data: st.DataObject) -> None:
    vectors = data.draw(matrices(dim, 50))
    result = unwrap(wrap({"shape": [len(vectors), dim], "data": vectors}))
    assert parse_embeddings(result, expected=len(vectors), dim=dim) == vectors


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large], max_examples=20)
@given(vectors=matrices(CONFIGURED_DIM, 4))
def test_embeddings_round_trip_at_the_configured_dimension(vectors: list[list[float]]) -> None:
    result = unwrap(wrap({"shape": [len(vectors), CONFIGURED_DIM], "data": vectors}))
    assert parse_embeddings(result, expected=len(vectors), dim=CONFIGURED_DIM) == vectors


@given(
    vectors=matrices(3, 10),
    ints=st.booleans(),
)
def test_embeddings_accept_integer_values_as_floats(vectors: list[list[float]], ints: bool) -> None:
    # JSON has one number type: a whole value may arrive as ``2`` and must come back as 2.0.
    sent = [[int(x) if ints and x == int(x) else x for x in row] for row in vectors]
    result = unwrap(wrap({"shape": [len(sent), 3], "data": sent}))
    parsed = parse_embeddings(result, expected=len(sent), dim=3)
    assert parsed == vectors
    assert all(isinstance(x, float) for row in parsed for x in row)


# --- rerank ----------------------------------------------------------------------------------


@given(data=st.data(), n=st.integers(min_value=1, max_value=50))
def test_rerank_scores_come_back_in_input_order(data: st.DataObject, n: int) -> None:
    scores = data.draw(st.lists(finite, min_size=n, max_size=n))
    order = data.draw(st.permutations(range(n)))
    entries = [{"id": i, "score": scores[i]} for i in order]
    result = unwrap(wrap({"response": entries}))
    assert parse_rerank(result, n) == scores


# --- text ------------------------------------------------------------------------------------


@given(
    text=st.text(),
    prompt=counts,
    completion=counts,
    reasoning=st.none() | counts,
    finish_reason=st.none() | st.sampled_from(["stop", "length", "tool_calls"]),
)
@example(text="", prompt=0, completion=0, reasoning=None, finish_reason="length")
def test_text_round_trips_in_the_openai_shape(
    text: str,
    prompt: int,
    completion: int,
    reasoning: int | None,
    finish_reason: str | None,
) -> None:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    choice: dict[str, Any] = {"index": 0, "message": {"role": "assistant", "content": text}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    result = unwrap(wrap({"choices": [choice], "usage": usage}))
    assert parse_text(result) == TextResult(
        text=text,
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning or 0,
        finish_reason=finish_reason,
    )


@given(text=st.text(), prompt=counts, completion=counts)
def test_text_round_trips_in_the_legacy_response_shape(
    text: str, prompt: int, completion: int
) -> None:
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    result = unwrap(wrap({"response": text, "usage": usage}))
    assert parse_text(result) == TextResult(
        text=text, prompt_tokens=prompt, completion_tokens=completion
    )


@given(
    result_messages=messages,
    text=st.text(max_size=40),
)
def test_envelope_messages_do_not_change_the_result(result_messages: list[str], text: str) -> None:
    result = {"response": text, "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
    body = json.loads(
        json.dumps({"result": result, "success": True, "errors": [], "messages": result_messages})
    )
    assert parse_text(unwrap(body)) == TextResult(text, 1, 2)
