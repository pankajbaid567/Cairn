"""Property test for the S2 lexical query literal (free-tier retarget design §5, Requirement 3.3).

**Validates: Requirements 3.3**

For any non-empty list of query terms (arbitrary Unicode with quotes, backslashes, ``or``,
``-`` and SQL keywords mixed in) and any group list, the statement for ``fts``,
``fts_explicit_acl``, ``hybrid_rrf`` and ``hybrid_rrf_explicit_acl`` calls
``websearch_to_tsquery('simple', …)`` and ``ts_rank_cd``, carries the joined terms in exactly
one SQL string literal, and adds the ACL predicate if and only if the variant is explicit.

"Exactly once and only inside one literal" is checked by position: the rendered statement is
split into string literals and the SQL around them. The SQL around the literals must be the
same as for a fixed reference term, so no term can change the statement's structure, and
exactly one literal slot differs from the reference and decodes to the joined terms. A plain
substring count would be wrong here, because a term such as ``id`` or ``simple`` also occurs
in the statement's own text. No database is needed: the statements are rendered with
``as_string()`` and no connection.
"""
# Feature: cairn-free-tier-retarget, Property 2: Lexical queries carry the terms as one literal

from __future__ import annotations

import re

from hypothesis import example, given
from hypothesis import strategies as st

from cairn_eval.spikes.s2_rls_explain.queries import (
    TS_CONFIG,
    build_query,
    is_explicit,
    lexical_text,
)

LEXICAL_VARIANTS = ("fts", "fts_explicit_acl", "hybrid_rrf", "hybrid_rrf_explicit_acl")
VECTOR = (0.5, -0.25)
REFERENCE_TERMS = ("reference",)
PLACEHOLDER = "?"

# Postgres text cannot hold NUL, and surrogates cannot be encoded, so both are left out.
_CHARS = st.characters(exclude_categories=["Cs"], exclude_characters="\x00")
_TRICKY = st.sampled_from(
    [
        "'",
        "''",
        "\\",
        "\\'",
        "E'x'",
        "or",
        "OR",
        "-",
        "-t3_1",
        '"quoted phrase"',
        "t3_5",
        "simple",
        "id",
        "$$",
        "%s",
        "{}",
        "x'); DROP TABLE s2.chunks; --",
        "SELECT 1; --",
        "/* c */",
        "é—😀",
        "\n\t",
    ]
)
TERM = st.one_of(_TRICKY, st.text(_CHARS, max_size=20), st.lists(_TRICKY, max_size=4).map("".join))
TERMS = st.lists(TERM, min_size=1, max_size=6)
GROUPS = st.lists(st.integers(min_value=0, max_value=2**31 - 1), max_size=5)


def split_literals(text: str) -> tuple[str, list[str]]:
    """Split rendered SQL into the text outside string literals and the decoded literals.

    Each literal is replaced by ``PLACEHOLDER`` in the returned text. Handles the two forms
    psycopg emits: ``'…'`` with doubled quotes, and `` E'…'`` (used when the value holds a
    backslash) with doubled quotes and doubled backslashes. Whitespace runs outside literals
    are collapsed, since psycopg puts a space before ``E'``.
    """
    outside: list[str] = []
    literals: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch != "'":
            outside.append(ch)
            i += 1
            continue
        prefixed = bool(outside) and outside[-1] == "E"
        escaped = prefixed and (len(outside) < 2 or not _word(outside[-2]))
        if escaped:
            outside.pop()
        value: list[str] = []
        i += 1
        while True:
            assert i < n, f"unterminated string literal in {text!r}"
            ch = text[i]
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    value.append("'")
                    i += 2
                    continue
                i += 1
                break
            if escaped and ch == "\\":
                assert i + 1 < n, f"unterminated escape in {text!r}"
                assert text[i + 1] in "\\'", f"unexpected escape in {text!r}"
                value.append(text[i + 1])
                i += 2
                continue
            value.append(ch)
            i += 1
        literals.append("".join(value))
        outside.append(PLACEHOLDER)
    return re.sub(r"\s+", " ", "".join(outside)), literals


def _word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _render(variant: str, terms: list[str] | tuple[str, ...], groups: list[int]) -> str:
    return build_query(variant, terms=terms, vector=VECTOR, groups=groups).as_string()


@given(variant=st.sampled_from(LEXICAL_VARIANTS), terms=TERMS, groups=GROUPS)
@example(variant="fts", terms=["it's", "a\\b"], groups=[])
@example(variant="hybrid_rrf_explicit_acl", terms=["simple"], groups=[1000, 1001])
@example(variant="fts_explicit_acl", terms=["x') OR acl && '{1}'::int[] --"], groups=[7])
def test_lexical_queries_carry_the_terms_as_one_literal(
    variant: str, terms: list[str], groups: list[int]
) -> None:
    text = _render(variant, terms, groups)
    skeleton, literals = split_literals(text)
    ref_skeleton, ref_literals = split_literals(_render(variant, REFERENCE_TERMS, groups))
    joined = lexical_text(terms)

    # The terms cannot change the statement's structure: same SQL around the literals.
    assert skeleton == ref_skeleton
    assert len(literals) == len(ref_literals)

    # Exactly one literal slot carries the terms, and it decodes to the joined terms.
    slots = [i for i, value in enumerate(ref_literals) if value == lexical_text(REFERENCE_TERMS)]
    assert len(slots) == 1
    (slot,) = slots
    assert literals[slot] == joined
    assert [v for i, v in enumerate(literals) if i != slot] == [
        v for i, v in enumerate(ref_literals) if i != slot
    ]

    # That slot is the query argument of websearch_to_tsquery('simple', …), ranked by ts_rank_cd.
    assert skeleton.count("websearch_to_tsquery(") == 1
    assert skeleton.count("ts_rank_cd(") == 1
    call = f"websearch_to_tsquery({PLACEHOLDER}, {PLACEHOLDER})"
    assert call in skeleton
    before_call = skeleton[: skeleton.index(call)].count(PLACEHOLDER)
    assert literals[before_call] == TS_CONFIG
    assert before_call + 1 == slot

    # The ACL predicate is present if and only if the variant is explicit.
    group_value = "{" + ",".join(str(g) for g in groups) + "}"
    predicate = f"acl && {PLACEHOLDER}::int[]"
    has_predicate = predicate in skeleton
    assert has_predicate is is_explicit(variant)
    if has_predicate:
        positions = [m.start() for m in re.finditer(re.escape(predicate), skeleton)]
        for pos in positions:
            idx = skeleton[: pos + len(predicate)].count(PLACEHOLDER) - 1
            assert literals[idx] == group_value
