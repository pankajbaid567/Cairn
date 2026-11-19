"""Property tests for role statements in both admin modes (design Property 6).

Feature: cairn-free-tier-retarget, Property 6: Role statements carry the right attributes for
their mode and never a plaintext password

Validates: Requirements 5.4, 5.5, 5.9

Everything runs without a database: statements are rendered with ``as_string()`` and no
connection. Passwords are printable ASCII (quotes, backslashes, ``$``, ``%``, braces and
spaces included) and carry a marker ``~mk-<16 hex>``. ``~`` is not a base64 character and
occurs in no fixed SQL text, so the marker cannot appear in a verifier or in the attribute
list by chance.

The first property covers the default password form ``verifier`` in both modes. The second
covers the OQ-1 plaintext form (managed mode with ``--allow-plaintext-password``): it appears
only with ``form="plaintext"`` in managed mode, and every other combination refuses.
"""

from __future__ import annotations

import re
from typing import get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from psycopg import sql

from cairn_core.bootstrap import (
    APP_ROLE,
    DBOS_ROLE,
    MANAGED_ROLES,
    OWNER_ROLE,
    AdminMode,
    BootstrapError,
    PlaintextRefused,
    is_scram_verifier,
    password_form,
    role_statements,
    scram_sha256_verifier,
)
from cairn_core.config import SslMode

# Literal copies of the expectation (requirements 5.4, 5.5), not imported, so a change in the
# module under test cannot change them.
ALL_ATTRIBUTES = (
    "LOGIN",
    "NOSUPERUSER",
    "NOCREATEDB",
    "NOCREATEROLE",
    "NOREPLICATION",
    "NOBYPASSRLS",
)
FORBIDDEN_WORDS = ("SUPERUSER", "REPLICATION", "BYPASSRLS")
MARKER_RE = re.compile(r"~mk-[0-9a-f]{16}")
PASSWORD_SPLIT = " PASSWORD "

MODES: tuple[AdminMode, ...] = ("superuser", "managed")

PBT = settings(deadline=None)

# --- strategies --------------------------------------------------------------------------

_printable = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), min_size=0, max_size=24
)


@st.composite
def three_passwords(draw: st.DrawFn) -> dict[str, str]:
    """One printable ASCII password per role, each with its own distinct marker."""
    ids = draw(
        st.lists(
            st.binary(min_size=8, max_size=8),
            min_size=len(MANAGED_ROLES),
            unique=True,
            max_size=len(MANAGED_ROLES),
        )
    )
    return {
        role: draw(_printable) + "~mk-" + ident.hex() + draw(_printable)
        for role, ident in zip(MANAGED_ROLES, ids, strict=True)
    }


existing_roles = st.sets(st.sampled_from(MANAGED_ROLES))
modes = st.sampled_from(MODES)


def marker_of(password: str) -> str:
    found = MARKER_RE.search(password)
    assert found is not None, "every generated password carries a marker"
    return found.group(0)


def encrypt(password: str, _role: str) -> str:
    return scram_sha256_verifier(password)


def render(stmts: list[sql.Composed]) -> list[str]:
    return [s.as_string() for s in stmts]


def split_statement(text: str) -> tuple[list[str], str]:
    """The attribute tokens before PASSWORD, and the text from PASSWORD on."""
    head, sep, tail = text.partition(PASSWORD_SPLIT)
    assert sep, f"statement has no PASSWORD clause: {text!r}"
    return head.split(), tail


# --- property: verifier form, both modes -------------------------------------------------


@PBT
@given(pw=three_passwords(), existing=existing_roles, mode=modes)
def test_verifier_statements_carry_mode_attributes_and_no_plaintext(
    pw: dict[str, str], existing: set[str], mode: AdminMode
) -> None:
    """Feature: cairn-free-tier-retarget, Property 6: Role statements carry the right
    attributes for their mode and never a plaintext password.

    **Validates: Requirements 5.4, 5.5, 5.9**
    """
    stmts = render(role_statements(pw, existing, encrypt, mode=mode, form="verifier"))

    assert len(stmts) == len(MANAGED_ROLES)
    for role, text in zip(MANAGED_ROLES, stmts, strict=True):
        tokens, tail = split_statement(text)
        verb, noun, name, with_kw, *attrs = tokens
        is_alter = role in existing
        assert (verb, noun, with_kw) == ("ALTER" if is_alter else "CREATE", "ROLE", "WITH")
        assert name == f'"{role}"'

        # NOINHERIT if and only if the role is cairn_app (INHERIT otherwise).
        inherit = [a for a in attrs if a in ("INHERIT", "NOINHERIT")]
        assert inherit == (["NOINHERIT"] if role == APP_ROLE else ["INHERIT"])
        flags = [a for a in attrs if a not in ("INHERIT", "NOINHERIT")]

        if not is_alter or mode == "superuser":
            # CREATE (any mode) and ALTER in superuser mode name every attribute.
            assert flags == list(ALL_ATTRIBUTES)
        else:
            # ALTER in managed mode never names SUPERUSER, REPLICATION or BYPASSRLS,
            # with or without the NO prefix.
            for word in FORBIDDEN_WORDS:
                assert not any(word in flag for flag in flags), (role, flags)
            assert flags == ["LOGIN", "NOCREATEDB", "NOCREATEROLE"]

        # The password is a SCRAM-SHA-256 verifier; the plaintext is nowhere in the text.
        literal = tail.strip()
        assert literal.startswith("'SCRAM-SHA-256$")
        assert literal.endswith("'")
        assert is_scram_verifier(literal[1:-1])
        assert pw[role] not in text
        assert marker_of(pw[role]) not in text

    joined = "\n".join(stmts)
    for password in pw.values():
        assert marker_of(password) not in joined


# --- property: plaintext form only in managed mode ---------------------------------------


@PBT
@given(pw=three_passwords(), existing=existing_roles)
def test_plaintext_form_appears_only_with_plaintext_form_in_managed_mode(
    pw: dict[str, str], existing: set[str]
) -> None:
    """Feature: cairn-free-tier-retarget, Property 6: Role statements carry the right
    attributes for their mode and never a plaintext password (OQ-1 plaintext form).

    **Validates: Requirements 5.4, 5.5, 5.9**
    """
    # Managed mode with the plaintext form: the password is in its own statement only.
    plain = render(role_statements(pw, existing, None, mode="managed", form="plaintext"))
    for role, text in zip(MANAGED_ROLES, plain, strict=True):
        assert marker_of(pw[role]) in text
        for other in MANAGED_ROLES:
            if other != role:
                assert marker_of(pw[other]) not in text
        assert "SCRAM-SHA-256" not in text
        for word in FORBIDDEN_WORDS:
            if role in existing:
                head, _ = split_statement(text)
                assert not any(word in token for token in head)

    # The default form, in either mode, never carries the plaintext.
    for mode in MODES:
        default = "\n".join(render(role_statements(pw, existing, encrypt, mode=mode)))
        for password in pw.values():
            assert marker_of(password) not in default

    # Superuser mode refuses the plaintext form without echoing any password.
    with pytest.raises(PlaintextRefused) as refused:
        role_statements(pw, existing, None, mode="superuser", form="plaintext")
    for password in pw.values():
        assert marker_of(password) not in str(refused.value)


@PBT
@given(
    allow=st.booleans(),
    mode=modes,
    sslmode=st.sampled_from(get_args(SslMode)),
)
def test_password_form_is_plaintext_only_for_flag_managed_verify_full(
    allow: bool, mode: AdminMode, sslmode: SslMode
) -> None:
    """Feature: cairn-free-tier-retarget, Property 6: Role statements carry the right
    attributes for their mode and never a plaintext password (form selection).

    **Validates: Requirements 5.9**
    """
    if not allow:
        assert password_form(mode, allow_plaintext=allow, sslmode=sslmode) == "verifier"
    elif mode == "managed" and sslmode == "verify-full":
        assert password_form(mode, allow_plaintext=allow, sslmode=sslmode) == "plaintext"
    else:
        with pytest.raises(PlaintextRefused):
            password_form(mode, allow_plaintext=allow, sslmode=sslmode)


# --- examples ----------------------------------------------------------------------------


def test_example_managed_alter_for_app_role_keeps_noinherit() -> None:
    pw = {OWNER_ROLE: "~mk-00", APP_ROLE: "~mk-01", DBOS_ROLE: "~mk-02"}
    stmts = render(role_statements(pw, {APP_ROLE}, encrypt, mode="managed", form="verifier"))
    app = stmts[MANAGED_ROLES.index(APP_ROLE)]
    assert app.startswith('ALTER ROLE "cairn_app" WITH LOGIN NOCREATEDB NOCREATEROLE NOINHERIT')
    assert stmts[MANAGED_ROLES.index(OWNER_ROLE)].startswith(
        'CREATE ROLE "cairn_owner" WITH ' + " ".join(ALL_ATTRIBUTES) + " INHERIT PASSWORD 'SCRAM"
    )


def test_example_verifier_form_rejects_a_plaintext_secret() -> None:
    pw = {role: f"~mk-{i:02d}" for i, role in enumerate(MANAGED_ROLES)}
    with pytest.raises(BootstrapError, match="verifier"):
        role_statements(pw, set(), None, mode="managed", form="verifier")
