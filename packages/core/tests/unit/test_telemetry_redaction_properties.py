"""Property test for log redaction (task 2.5).

Feature: cairn-phase-0-foundations, Property 7: Secret-like log fields are redacted

**Validates: Requirements 9.3, 5.8**

Generators build each input together with the output it must produce, so the tests never
call ``is_secret_key`` to decide what to expect:

* ``secret_keys``: a secret name (password, token, api key, ...) spelled with random case,
  separators or camelCase, plus random prefix and suffix words.
* ``plain_keys``: ordinary words, including names the module deliberately keeps
  (``role_bypasses_rls``, ``passed``, ``monkey``, ``bypass``).
* ``trees``: dicts, lists and tuples up to four levels deep. A secret key holds a value that
  contains ``SECRETVAL_`` markers (a string, list or dict) and must come out as ``[REDACTED]``;
  every other value carries a ``plainval_`` marker or a number and must come out unchanged.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cairn_core.telemetry import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    is_secret_key,
    redact_fields,
)

SECRET_MARK = "SECRETVAL_"
PLAIN_MARK = "plainval_"

Pair = tuple[Any, Any]  # (value as logged, value expected after redaction)

# Secret names, as words. "pass" and "key" only count as whole words, so they are
# never joined to a neighbour without a separator or a case change.
SECRET_NAMES: list[tuple[str, ...]] = [
    ("password",),
    ("passwd",),
    ("passphrase",),
    ("pwd",),
    ("secret",),
    ("token",),
    ("authorization",),
    ("credential",),
    ("credentials",),
    ("cookie",),
    ("dsn",),
    ("apikey",),
    ("api", "key"),
    ("access", "key"),
    ("private", "key"),
    ("signing", "key"),
    ("encryption", "key"),
    ("auth", "key"),
    ("pass",),
    ("key",),
]
WHOLE_WORD_NAMES = {("pass",), ("key",)}
PREFIX_WORDS = ["user", "db", "refresh", "client", "request", "my", "x", "new", "old", "app"]
SUFFIX_WORDS = ["id", "value", "header", "hash", "v2", "name"]

# Ordinary words. The last group looks secret-like as a substring but must be kept.
ORDINARY_WORDS = [
    "user",
    "status",
    "count",
    "path",
    "method",
    "role",
    "size",
    "region",
    "attempt",
    "duration",
    "bypasses",
    "rls",
    "passed",
    "bypass",
    "bypassed",
    "monkey",
    "turkey",
    "keyboard",
    "passenger",
    "passport",
    "compass",
]

RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


@st.composite
def spell(draw: st.DrawFn, words: list[str], *, allow_empty_sep: bool) -> str:
    """Join ``words`` with a random separator or case convention and random casing."""
    style = draw(st.sampled_from(["sep", "camel", "pascal"]))
    if style == "camel":
        return words[0].lower() + "".join(w.capitalize() for w in words[1:])
    if style == "pascal":
        return "".join(w.capitalize() for w in words)
    seps = ["_", "-", ".", "__", " ", "/", ":"] + ([""] if allow_empty_sep else [])
    sep = draw(st.sampled_from(seps))
    cases = [str.lower, str.upper, str.capitalize]
    return sep.join(draw(st.sampled_from(cases))(w) for w in words)


@st.composite
def secret_keys(draw: st.DrawFn) -> str:
    name = draw(st.sampled_from(SECRET_NAMES))
    prefix = draw(st.lists(st.sampled_from(PREFIX_WORDS), max_size=2))
    suffix = draw(st.lists(st.sampled_from(SUFFIX_WORDS), max_size=2))
    return draw(spell([*prefix, *name, *suffix], allow_empty_sep=name not in WHOLE_WORD_NAMES))


@st.composite
def _plain_keys(draw: st.DrawFn) -> str:
    words = draw(st.lists(st.sampled_from(ORDINARY_WORDS), min_size=1, max_size=3))
    return draw(spell(words, allow_empty_sep=True))


plain_keys = _plain_keys().filter(lambda k: k not in RECORD_ATTRS and not k.startswith("exc"))

secret_markers = st.text("0123456789abcdef", min_size=8, max_size=16).map(
    lambda s: f"{SECRET_MARK}{s}"
)
plain_markers = st.text("0123456789abcdef", min_size=8, max_size=16).map(
    lambda s: f"{PLAIN_MARK}{s}"
)

# Anything a caller might log under a secret key: it is replaced as a whole.
secret_payloads = st.recursive(
    secret_markers,
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(plain_keys, inner, max_size=3)
        | st.dictionaries(secret_keys(), inner, max_size=2)
    ),
    max_leaves=6,
)

plain_leaves: st.SearchStrategy[Pair] = st.one_of(
    plain_markers, st.integers(-1000, 1000), st.booleans(), st.none()
).map(lambda v: (v, v))


def _mapping(children: st.SearchStrategy[Pair], *, with_secrets: bool) -> st.SearchStrategy[Pair]:
    plain_entries = st.tuples(plain_keys, children).map(lambda t: (t[0], t[1][0], t[1][1]))
    secret_entries = st.tuples(secret_keys(), secret_payloads).map(lambda t: (t[0], t[1], REDACTED))
    entries = st.one_of(plain_entries, secret_entries) if with_secrets else plain_entries

    def build(items: list[tuple[str, Any, Any]]) -> Pair:
        given: dict[str, Any] = {}
        expected: dict[str, Any] = {}
        for key, value_in, value_out in items:
            given[key] = value_in
            expected[key] = value_out
        return given, expected

    return st.lists(entries, max_size=4).map(build)


def _sequence(children: st.SearchStrategy[Pair], *, as_tuple: bool) -> st.SearchStrategy[Pair]:
    def build(pairs: list[Pair]) -> Pair:
        given = [p[0] for p in pairs]
        return (tuple(given) if as_tuple else given), [p[1] for p in pairs]

    return st.lists(children, max_size=3).map(build)


def _trees(*, with_secrets: bool, levels: int = 3) -> st.SearchStrategy[Pair]:
    """Leaves wrapped ``levels`` times in dicts, lists and tuples (so at most four deep
    with the top-level extras dict, well inside the redaction depth limit)."""
    tree = plain_leaves
    for _ in range(levels):
        tree = st.one_of(
            tree,
            _mapping(tree, with_secrets=with_secrets),
            _sequence(tree, as_tuple=False),
            _sequence(tree, as_tuple=True),
        )
    return tree


# Top-level extras: a dict of field name to value, built like any nested dict.
mixed_extras = _mapping(_trees(with_secrets=True), with_secrets=True)
plain_extras = _mapping(_trees(with_secrets=False), with_secrets=False)


def make_record(**extra: Any) -> logging.LogRecord:
    record = logging.LogRecord("cairn.test", logging.INFO, __file__, 1, "event", None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def extras_of(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in RECORD_ATTRS}


def standard_attrs(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k in RECORD_ATTRS}


def contains_marker(value: Any) -> bool:
    """True if any string anywhere in ``value`` (keys included) holds the secret marker."""
    if isinstance(value, str):
        return SECRET_MARK in value
    if isinstance(value, dict):
        return any(contains_marker(k) or contains_marker(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(contains_marker(v) for v in value)
    return False


# --- Property 7 -------------------------------------------------------------------------


@given(extras=mixed_extras)
def test_formatted_line_redacts_secret_fields_and_keeps_the_rest(extras: Pair) -> None:
    """Feature: cairn-phase-0-foundations, Property 7: Secret-like log fields are redacted.

    **Validates: Requirements 9.3, 5.8**
    """
    given_fields, expected = extras
    line = JsonFormatter("svc").format(make_record(**given_fields))

    assert "\n" not in line
    assert SECRET_MARK not in line
    out = json.loads(line)
    assert set(out) == {"ts", "level", "logger", "msg", "service"} | set(expected)
    for key, value in expected.items():
        assert out[key] == value


@given(extras=mixed_extras)
def test_redact_fields_matches_expected_and_leaves_its_input_alone(extras: Pair) -> None:
    """Feature: cairn-phase-0-foundations, Property 7: Secret-like log fields are redacted.

    **Validates: Requirements 9.3, 5.8**
    """
    given_fields, expected = extras
    before = copy.deepcopy(given_fields)

    result = redact_fields(given_fields)

    assert result == expected
    assert not contains_marker(result)
    assert given_fields == before  # not mutated, secrets still in the caller's data


@given(extras=mixed_extras)
def test_filter_edits_only_extras_and_never_drops_the_record(extras: Pair) -> None:
    """Feature: cairn-phase-0-foundations, Property 7: Secret-like log fields are redacted.

    **Validates: Requirements 9.3, 5.8**
    """
    given_fields, expected = extras
    record = make_record(**given_fields)
    standard_before = copy.deepcopy(standard_attrs(record))
    line_before = JsonFormatter("svc").format(record)

    assert RedactionFilter().filter(record) is True

    assert standard_attrs(record) == standard_before
    assert extras_of(record) == expected
    assert not contains_marker(extras_of(record))
    # The formatter output does not change: the filter and the formatter agree.
    assert JsonFormatter("svc").format(record) == line_before
    # Idempotent.
    assert RedactionFilter().filter(record) is True
    assert extras_of(record) == expected


@given(extras=plain_extras)
def test_ordinary_keys_keep_their_values_unchanged(extras: Pair) -> None:
    """Feature: cairn-phase-0-foundations, Property 7: Secret-like log fields are redacted.

    **Validates: Requirements 9.3, 5.8**
    """
    given_fields, expected = extras
    assert all(not is_secret_key(k) for k in given_fields)

    assert redact_fields(given_fields) == expected
    record = make_record(**given_fields)
    RedactionFilter().filter(record)
    assert extras_of(record) == expected
    out = json.loads(JsonFormatter("svc").format(record))
    for key, value in expected.items():
        assert out[key] == value
        assert value != REDACTED


@given(key=secret_keys())
def test_every_spelling_of_a_secret_name_is_a_secret_key(key: str) -> None:
    """Feature: cairn-phase-0-foundations, Property 7: Secret-like log fields are redacted.

    **Validates: Requirements 9.3, 5.8**
    """
    assert is_secret_key(key)


@given(key=plain_keys)
def test_ordinary_and_lookalike_keys_are_not_secret_keys(key: str) -> None:
    """Feature: cairn-phase-0-foundations, Property 7: Secret-like log fields are redacted.

    **Validates: Requirements 9.3, 5.8**
    """
    assert not is_secret_key(key)


@pytest.mark.parametrize(
    "key",
    ["role_bypasses_rls", "ROLE_BYPASSES_RLS", "roleBypassesRls", "passed", "Passed", "monkey"],
)
def test_lookalike_keys_the_module_keeps_stay_visible(key: str) -> None:
    fields = {key: "visible"}
    assert redact_fields(fields) == fields
    out = json.loads(JsonFormatter("svc").format(make_record(**fields)))
    assert out[key] == "visible"


# --- Nesting limit ----------------------------------------------------------------------


def _nest(levels: int, marker: str) -> Any:
    value: Any = {"password": marker}
    for i in range(levels):
        value = {"level": value} if i % 2 == 0 else [value]
    return value


@given(levels=st.integers(0, 14), marker=secret_markers)
def test_a_secret_never_survives_any_nesting_depth(levels: int, marker: str) -> None:
    """Feature: cairn-phase-0-foundations, Property 7: Secret-like log fields are redacted.

    Past the depth limit a value may be truncated, but it is never passed through.

    **Validates: Requirements 9.3, 5.8**
    """
    fields = {"payload": _nest(levels, marker)}

    assert not contains_marker(redact_fields(fields))
    record = make_record(**copy.deepcopy(fields))
    RedactionFilter().filter(record)
    assert not contains_marker(extras_of(record))
    assert marker not in JsonFormatter("svc").format(make_record(**fields))
