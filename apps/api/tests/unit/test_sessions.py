"""Signed session cookies: tampering, expiry and malformed input all give nothing (PD1-15)."""

from __future__ import annotations

import base64
import json

import pytest

from cairn_api.sessions import sign, verify

SECRET = b"s" * 40


def test_a_token_round_trips_to_the_persona_until_it_expires() -> None:
    t = sign("asha", SECRET, 600, now=1000.0)
    assert verify(t, SECRET, now=1100.0) == "asha"
    assert verify(t, SECRET, now=1601.0) is None


@pytest.mark.parametrize("token", [None, "", ".", "a.b", "a.b.c", "x" * 600, "no-dot-at-all"])
def test_malformed_tokens_give_nothing(token: str | None) -> None:
    assert verify(token, SECRET) is None


def test_a_wrong_secret_or_flipped_mac_gives_nothing() -> None:
    t = sign("asha", SECRET, 600)
    assert verify(t, b"o" * 40) is None
    body, mac = t.split(".")
    flipped = body + "." + ("A" if mac[0] != "A" else "B") + mac[1:]
    assert verify(flipped, SECRET) is None


def test_changing_the_persona_in_the_body_breaks_the_signature() -> None:
    t = sign("ben", SECRET, 600)
    body, mac = t.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["p"] = "priya"
    forged = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=")
    assert verify(forged.decode() + "." + mac, SECRET) is None


def test_a_signed_payload_of_the_wrong_shape_is_refused() -> None:
    from cairn_api import sessions

    for payload in (
        ["p"],
        {"p": 5, "exp": 9_999_999_999},
        {"p": "asha"},
        {"p": "asha", "exp": "soon"},
    ):
        body = sessions._b64(json.dumps(payload).encode())
        assert verify(f"{body}.{sessions._mac(SECRET, body)}", SECRET) is None
