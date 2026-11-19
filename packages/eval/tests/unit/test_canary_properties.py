"""Property tests for the canary token generator (design §11, requirements 12.1, 12.2).

**Validates: Requirements 12.1, 12.2**
"""

# Feature: cairn-phase-0-foundations, Property 9: Canary tokens are deterministic, well-formed and distinct  # noqa: E501

import base64
import hashlib
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cairn_eval.scenario.canary import CANARY_FAMILIES, CANARY_RE, canary_token

SEPARATOR = "\x1f"

# Arbitrary non-empty Unicode text (Hypothesis excludes lone surrogates, which cannot be
# UTF-8 encoded). The unit separator is boosted so field-boundary cases show up often.
_text = st.text(min_size=1, max_size=40)
_text_with_separators = st.text(
    alphabet=st.one_of(st.characters(exclude_categories=["Cs"]), st.just(SEPARATOR)),
    min_size=1,
    max_size=40,
)
seeds = st.one_of(_text, _text_with_separators)
canary_ids = st.one_of(_text, _text_with_separators)
# Seeds that cannot hide a field boundary: with no separator inside the seed, the pair
# (seed, id) is recoverable from the hashed material, so distinct pairs must differ.
separator_free_seeds = st.text(
    alphabet=st.characters(exclude_categories=["Cs"], exclude_characters=SEPARATOR),
    min_size=1,
    max_size=40,
)
families = st.sampled_from(CANARY_FAMILIES)


def _expected(seed: str, canary_id: str, family: str) -> str:
    """The documented derivation, written out independently of the implementation."""
    material = b"cairn-canary" + b"\x1f" + seed.encode() + b"\x1f" + canary_id.encode()
    digest = hashlib.sha256(material).digest()
    return "KP-" + family.upper() + "-" + base64.b32encode(digest).decode()[:16]


@given(seed=seeds, canary_id=canary_ids, family=families)
def test_token_is_deterministic(seed: str, canary_id: str, family: str) -> None:
    first = canary_token(seed, canary_id, family)
    assert canary_token(seed, canary_id, family) == first
    assert canary_token(seed, canary_id, family) == first


@given(seed=seeds, canary_id=canary_ids, family=families)
def test_token_is_well_formed(seed: str, canary_id: str, family: str) -> None:
    token = canary_token(seed, canary_id, family)
    assert CANARY_RE.fullmatch(token)
    prefix = f"KP-{family.upper()}-"
    assert token.startswith(prefix)
    assert re.fullmatch(r"[A-Z2-7]{16}", token[len(prefix) :])


@given(seed=seeds, canary_id=canary_ids, family=families)
def test_token_equals_the_documented_derivation(seed: str, canary_id: str, family: str) -> None:
    assert canary_token(seed, canary_id, family) == _expected(seed, canary_id, family)


@given(seed=seeds, ids=st.sets(canary_ids, min_size=2, max_size=30), family=families)
def test_distinct_ids_under_one_seed_give_distinct_tokens(
    seed: str, ids: set[str], family: str
) -> None:
    tokens = {canary_token(seed, cid, family) for cid in ids}
    assert len(tokens) == len(ids)


@given(
    canary_id=canary_ids,
    seed_set=st.sets(seeds, min_size=2, max_size=30),
    family=families,
)
def test_distinct_seeds_give_distinct_tokens_for_one_id(
    canary_id: str, seed_set: set[str], family: str
) -> None:
    tokens = {canary_token(seed, canary_id, family) for seed in seed_set}
    assert len(tokens) == len(seed_set)


@given(
    pairs=st.sets(st.tuples(separator_free_seeds, canary_ids), min_size=2, max_size=30),
    family=families,
)
def test_distinct_seed_and_id_pairs_give_distinct_tokens(
    pairs: set[tuple[str, str]], family: str
) -> None:
    tokens = {canary_token(seed, cid, family) for seed, cid in pairs}
    assert len(tokens) == len(pairs)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap: seed and id are joined with a bare 0x1F, so a separator inside either "
        "field makes the hashed material ambiguous. Changing the derivation would change "
        "every committed token, so it is reported in task-12.2-notes.md instead of fixed."
    ),
)
@given(
    prefix=separator_free_seeds,
    head=separator_free_seeds,
    tail=separator_free_seeds,
    family=families,
)
def test_field_boundary_shifts_do_not_collide(
    prefix: str, head: str, tail: str, family: str
) -> None:
    # (seed "p\x1fh", id "t") and (seed "p", id "h\x1ft") hash the same material.
    shifted_left = canary_token(f"{prefix}{SEPARATOR}{head}", tail, family)
    shifted_right = canary_token(prefix, f"{head}{SEPARATOR}{tail}", family)
    assert shifted_left != shifted_right


def test_field_boundary_collision_is_reproducible() -> None:
    """Pins the known gap with one concrete pair (see the xfail test above)."""
    assert canary_token("a\x1fb", "c", "hr") == canary_token("a", "b\x1fc", "hr")


_invalid_families = st.one_of(
    st.just(""),
    st.sampled_from([f.upper() for f in CANARY_FAMILIES]),
    st.sampled_from([f.capitalize() for f in CANARY_FAMILIES]),
    st.text(max_size=12).filter(lambda s: s not in CANARY_FAMILIES),
)


@given(seed=seeds, canary_id=canary_ids, family=_invalid_families)
def test_invalid_families_raise_value_error(seed: str, canary_id: str, family: str) -> None:
    with pytest.raises(ValueError, match="unknown canary family"):
        canary_token(seed, canary_id, family)
