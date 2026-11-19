"""Unit tests for the deterministic canary token generator (design §11, requirements 12.1, 12.2)."""

import base64
import hashlib

import pytest

from cairn_eval.scenario.canary import CANARY_FAMILIES, CANARY_RE, PLACEHOLDER_RE, canary_token

SEED = "kestrel-pay-v1"


def _expected(seed: str, canary_id: str, family: str) -> str:
    """The documented derivation, written out independently of the implementation."""
    digest = hashlib.sha256(b"cairn-canary\x1f" + seed.encode() + b"\x1f" + canary_id.encode())
    return "KP-" + family.upper() + "-" + base64.b32encode(digest.digest()).decode()[:16]


@pytest.mark.parametrize("family", CANARY_FAMILIES)
def test_token_matches_the_documented_derivation_and_format(family: str) -> None:
    token = canary_token(SEED, "CAN-HR-01", family)
    assert token == _expected(SEED, "CAN-HR-01", family)
    assert CANARY_RE.fullmatch(token)
    assert token.startswith(f"KP-{family.upper()}-")
    assert len(token) == len("KP-") + len(family) + len("-") + 16


def test_same_inputs_give_the_same_token() -> None:
    assert canary_token(SEED, "CAN-HR-01", "hr") == canary_token(SEED, "CAN-HR-01", "hr")


def test_different_ids_and_seeds_give_different_tokens() -> None:
    ids = [f"CAN-HR-{n:02d}" for n in range(1, 40)]
    tokens = {canary_token(SEED, cid, "hr") for cid in ids}
    assert len(tokens) == len(ids)
    assert canary_token("other-seed", "CAN-HR-01", "hr") != canary_token(SEED, "CAN-HR-01", "hr")


def test_unknown_family_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown canary family"):
        canary_token(SEED, "CAN-HR-01", "finance")
    with pytest.raises(ValueError, match="unknown canary family"):
        canary_token(SEED, "CAN-HR-01", "HR")  # families are lowercase


@pytest.mark.parametrize(
    "bad",
    [
        "KP-HR-ABCDEFGHIJKLMNO",  # 15 characters
        "KP-HR-ABCDEFGHIJKLMNOPQ",  # 17 characters
        "KP-HR-ABCDEFGHIJKLMNO1",  # '1' is not base32
        "KP-FINANCE-ABCDEFGHIJKLMNOP",  # unknown family
        "kp-hr-ABCDEFGHIJKLMNOP",  # case
        "xKP-HR-ABCDEFGHIJKLMNOP",
    ],
)
def test_canary_regex_rejects_malformed_tokens(bad: str) -> None:
    assert not CANARY_RE.fullmatch(bad)


def test_placeholder_regex_finds_canary_ids() -> None:
    text = "Incident ref {{canary:CAN-INC-01}} and {{canary:CAN-HR-02}}; not {{other:X}}."
    assert PLACEHOLDER_RE.findall(text) == ["CAN-INC-01", "CAN-HR-02"]
