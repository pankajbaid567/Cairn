"""Property test for the DBOS URL (task 4.2, requirements 7.2 and 4.4).

Feature: cairn-free-tier-retarget, Property 8: The DBOS URL round-trips the password and
carries the TLS settings

**Validates: Requirements 7.2, 4.4**

For any password (arbitrary Unicode, with the URL-reserved characters over-represented), host
(name, IPv4 or IPv6), port, database name, sslmode and optional sslrootcert, the URL returned
by ``dbos_url`` parses, with both ``urllib.parse`` and SQLAlchemy's ``make_url`` (which is how
DBOS reads it), back to the user ``cairn_dbos``, the same host, port and database, the exact
password, and a query holding ``sslmode`` always, ``sslrootcert`` exactly when set, and no
other key. When the password holds a reserved character, the user-info part of the URL holds
only its percent-encoded form.

Settings are built with ``model_validate`` and the field aliases, so neither the environment
nor a ``.env`` file takes part. ``max_examples`` comes from the profiles in the root
``conftest.py``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.engine import make_url

from cairn_core.config import SslMode, WorkerSettings
from cairn_worker.app import dbos_url

# Feature: cairn-free-tier-retarget, Property 8: The DBOS URL round-trips the password
# and carries the TLS settings

RESERVED = "@:/?#%+ &=;[]!$'()*,\\\"<>^`{|}"  # "~" is unreserved (RFC 3986)
USER = "cairn_dbos"

# Any Unicode text except lone surrogates (not encodable as UTF-8), at least one character.
unicode_text = st.text(alphabet=st.characters(exclude_categories=["Cs"]), min_size=1)
reserved_heavy = st.text(alphabet=st.sampled_from(RESERVED + "aZ09é€日😀"), min_size=1)
passwords = st.one_of(unicode_text, reserved_heavy)

hostnames = st.from_regex(
    r"[a-z]([a-z0-9-]{0,10}[a-z0-9])?(\.[a-z]([a-z0-9-]{0,10}[a-z0-9])?){0,3}", fullmatch=True
)
ipv4_hosts = st.ip_addresses(v=4).map(str)
ipv6_hosts = st.ip_addresses(v=6).map(str)
hosts = st.one_of(hostnames, ipv4_hosts, ipv6_hosts)

ports = st.integers(1, 65535)
database_names = st.from_regex(r"[a-z_][a-z0-9_-]{0,30}", fullmatch=True)
sslmodes: st.SearchStrategy[SslMode] = st.sampled_from(
    ["disable", "prefer", "require", "verify-ca", "verify-full"]
)
sslrootcerts = st.one_of(st.none(), unicode_text, reserved_heavy)


def build_settings(
    password: str, host: str, port: int, database: str, sslmode: str, sslrootcert: str | None
) -> WorkerSettings:
    values: dict[str, Any] = {
        "CAIRN_DB_HOST": host,
        "CAIRN_DB_PORT": port,
        "CAIRN_DBOS_DB_NAME": database,
        "CAIRN_DBOS_DB_PASSWORD": password,
        "CAIRN_DB_SSLMODE": sslmode,
    }
    if sslrootcert is not None:
        values["CAIRN_DB_SSLROOTCERT"] = sslrootcert
    return WorkerSettings.model_validate(values)  # no environment, no .env


@given(
    password=passwords,
    host=hosts,
    port=ports,
    database=database_names,
    sslmode=sslmodes,
    sslrootcert=sslrootcerts,
)
def test_dbos_url_round_trips_credentials_target_and_tls(
    password: str,
    host: str,
    port: int,
    database: str,
    sslmode: str,
    sslrootcert: str | None,
) -> None:
    url = dbos_url(build_settings(password, host, port, database, sslmode, sslrootcert))
    expected_query = {"sslmode": sslmode}
    if sslrootcert is not None:
        expected_query["sslrootcert"] = sslrootcert

    # urllib.parse
    parts = urlsplit(url)
    assert parts.scheme == "postgresql"
    assert parts.username == USER
    assert parts.password is not None
    assert unquote(parts.password) == password
    assert parts.hostname == host.lower()
    assert parts.port == port
    assert parts.path == f"/{database}"
    assert parts.fragment == ""
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    assert [key for key, _ in pairs] == list(expected_query)  # sslmode, then sslrootcert; no others
    assert dict(pairs) == expected_query

    # SQLAlchemy, the reader DBOS uses
    parsed = make_url(url)
    assert parsed.username == USER
    assert parsed.password == password
    assert parsed.host == host
    assert parsed.port == port
    assert parsed.database == database
    assert dict(parsed.query) == expected_query


@given(password=passwords.filter(lambda p: any(c in RESERVED for c in p)), host=hosts, port=ports)
def test_reserved_password_characters_appear_only_percent_encoded(
    password: str, host: str, port: int
) -> None:
    url = dbos_url(build_settings(password, host, port, "cairn_dbos", "prefer", None))

    # The user-info part runs from after "postgresql://" to the single "@" that ends it.
    userinfo, separator, _ = url.removeprefix("postgresql://").partition("@")
    assert separator == "@"
    assert userinfo == f"{USER}:{quote(password, safe='')}"
    encoded = userinfo.removeprefix(f"{USER}:")
    assert not any(c in encoded for c in "@:/?# +&=;[]!$'()*,\\\"<>^`{|}")
    # Every percent sign is the start of an escape: a literal "%" in the password became "%25".
    assert all(c in "0123456789ABCDEF" for part in encoded.split("%")[1:] for c in part[:2])
    if "%" in password:
        assert "%25" in encoded
