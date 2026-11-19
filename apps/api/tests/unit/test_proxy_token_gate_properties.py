"""Property test for the proxy token gate (task 5.5).

Feature: cairn-free-tier-retarget, Property 10: The proxy token gate admits exactly the
matching header and the exempt path
**Validates: Requirements 9.1, 9.2**

For any expected token (at least 32 characters, or none), any path, any method and any
presented header value (absent, empty, equal, one character off, a prefix, longer, other case,
unicode), the gate admits a request if and only if no token is configured, or the path is
exactly ``/healthz``, or the header equals the token. A refusal is always the same answer:
404 ``{"error": "not_found"}`` with ``Cache-Control: no-store``.

Three routes are covered:

* ``allowed`` directly, with unicode tokens, paths and header values.
* A minimal FastAPI app with ``proxy_token.install`` and a catch-all route, driven over ASGI
  with ``TestClient``: arbitrary decoded paths (percent-encoded on the wire), query strings,
  every common method. Tokens here are printable ASCII, because an HTTP header carries
  latin-1 and a unicode token could not be presented unchanged; unicode appears in the
  presented values, where it must never match.
* ``build_app`` with ``ApiSettings`` (the token passed explicitly, as the 5.2 notes require:
  a local ``.env`` may hold one), so the real wiring in ``cairn_api.main`` is covered.

Hypothesis does not reset function-scoped fixtures between examples, so these tests use none:
each example builds its own app. ``max_examples`` comes from the profiles in the root
``conftest.py``.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from cairn_api import proxy_token
from cairn_api.main import build_app
from cairn_api.proxy_token import HEADER
from cairn_core.config import ApiSettings
from cairn_core.health import DbProbe, ProbeResult

REFUSAL: Final = {"error": "not_found"}
METHODS: Final = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")

# --- strategies --------------------------------------------------------------------------

# Printable ASCII without space: survives an HTTP header unchanged.
ascii_tokens = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=32, max_size=64
)
# Any unicode text (no surrogates), for the pure function.
unicode_tokens = st.text(min_size=32, max_size=64)

# Characters that stay intact in a header and cannot be stripped as whitespace.
header_chars = st.characters(
    blacklist_categories=("Cc", "Cs", "Z"),
)
unicode_values = st.text(alphabet=header_chars, min_size=0, max_size=48)

SPECIAL_PATHS: Final = (
    "/healthz",
    "/healthz/",
    "/healthz/x",
    "/healthz/..",
    "/Healthz",
    "/HEALTHZ",
    "/healthz ",
    "/healthz%",
    "/healthzé",
    "/ healthz",
    "//healthz",
    "/healthz?x=1",
    "/healthz#f",
    "/readyz",
    "/",
    "",
    "/éléphant/日本語",
)


def other_char(ch: str) -> str:
    return "a" if ch != "a" else "b"


@st.composite
def header_values(draw: st.DrawFn, token: str) -> str | None:
    """None (header absent), or a value: equal, close to, or unrelated to ``token``."""
    kind = draw(
        st.sampled_from(
            ("missing", "empty", "equal", "one_off", "prefix", "longer", "case", "unicode")
        )
    )
    if kind == "missing":
        return None
    if kind == "empty":
        return ""
    if kind == "equal":
        return token
    if kind == "one_off":
        i = draw(st.integers(min_value=0, max_value=len(token) - 1))
        return token[:i] + other_char(token[i]) + token[i + 1 :]
    if kind == "prefix":
        return token[: draw(st.integers(min_value=0, max_value=len(token) - 1))]
    if kind == "longer":
        extra = st.text(
            alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=4
        )
        return token + draw(extra)
    if kind == "case":
        return token.swapcase()  # may equal the token when it has no letters; the oracle copes
    return draw(unicode_values)


@st.composite
def token_and_value(draw: st.DrawFn, tokens: st.SearchStrategy[str]) -> tuple[str, str | None]:
    token = draw(tokens)
    return token, draw(header_values(token))


@st.composite
def wire_paths(draw: st.DrawFn) -> str:
    """A decoded request path that httpx can send unchanged once percent-encoded."""
    if draw(st.booleans()):
        return draw(st.sampled_from(SPECIAL_PATHS))
    segments = draw(
        st.lists(
            st.one_of(
                st.sampled_from(("healthz", "Healthz", "readyz", "x", "é")),
                # No control characters: a newline does not match the catch-all route's
                # pattern, so the router would answer 404 itself. No "/" inside a segment,
                # so no "." or ".." segment forms (httpx removes those before sending).
                st.text(
                    alphabet=st.characters(blacklist_categories=("Cc", "Cs")), max_size=12
                ).filter(lambda s: s not in (".", "..") and "/" not in s),
            ),
            max_size=4,
        )
    )
    return "/" + "/".join(segments)


@st.composite
def free_paths(draw: st.DrawFn) -> str:
    return draw(st.one_of(st.sampled_from(SPECIAL_PATHS), st.text(max_size=24)))


def url_for(path: str, query: str) -> str:
    return "http://testserver" + quote(path or "/", safe="/") + query


queries = st.sampled_from(("", "?a=1", "?token=x", "?/healthz", "?x=%2Fhealthz", "?"))


def wire_headers(presented: str | None) -> dict[bytes, bytes]:
    """The request headers; a value goes out as UTF-8 bytes, which the app reads as latin-1."""
    return {} if presented is None else {HEADER.encode(): presented.encode("utf-8")}


def expected_admitted(path: str, presented: str | None, token: str | None) -> bool:
    """The oracle: written from Property 10, not from the implementation."""
    return token is None or path == "/healthz" or presented == token


# --- allowed() ---------------------------------------------------------------------------


@settings(deadline=None)
@given(data=token_and_value(unicode_tokens), path=free_paths())
def test_allowed_admits_exactly_the_matching_header_and_the_exempt_path(
    data: tuple[str, str | None], path: str
) -> None:
    token, presented = data
    assert allowed_for(path, presented, token) is expected_admitted(path, presented, token)


def allowed_for(path: str, presented: str | None, token: str | None) -> bool:
    expected = None if token is None else SecretStr(token)
    return proxy_token.allowed(path, presented, expected)


@settings(deadline=None)
@given(
    presented=st.one_of(st.none(), st.text(max_size=48)),
    path=free_paths(),
)
def test_allowed_passes_everything_when_no_token_is_configured(
    presented: str | None, path: str
) -> None:
    assert proxy_token.allowed(path, presented, None) is True


@settings(deadline=None)
@given(
    token=unicode_tokens,
    presented=st.one_of(st.none(), st.text(max_size=48)),
    suffix=st.sampled_from(("", "/", "/x", " ", "?q=1")),
)
def test_allowed_exempts_only_the_exact_liveness_path(
    token: str, presented: str | None, suffix: str
) -> None:
    path = "/healthz" + suffix
    got = allowed_for(path, presented, token)
    assert got is (suffix == "" or presented == token)


# --- minimal app, over ASGI --------------------------------------------------------------


def minimal_app(token: str | None) -> FastAPI:
    app = FastAPI()
    proxy_token.install(app, None if token is None else SecretStr(token))

    @app.api_route("/{rest:path}", methods=list(METHODS))
    async def everything(rest: str) -> dict[str, bool]:
        return {"ok": True}

    return app


def assert_refusal(response_status: int, headers: dict[str, str], body: bytes, method: str) -> None:
    assert response_status == 404
    assert headers.get("cache-control") == "no-store"
    if method != "HEAD":
        assert body == b'{"error":"not_found"}'


@settings(deadline=None)
@given(
    data=token_and_value(ascii_tokens),
    path=wire_paths(),
    query=queries,
    method=st.sampled_from(METHODS),
)
def test_app_admits_exactly_the_matching_header_and_the_exempt_path(
    data: tuple[str, str | None], path: str, query: str, method: str
) -> None:
    token, presented = data
    client = TestClient(minimal_app(token))
    headers = wire_headers(presented)
    response = client.request(method, url_for(path, query), headers=headers)

    if expected_admitted(path or "/", presented, token):
        assert response.status_code == 200
        if method != "HEAD":
            assert response.json() == {"ok": True}
    else:
        assert_refusal(response.status_code, dict(response.headers), response.content, method)
        if method != "HEAD":
            assert response.json() == REFUSAL


@settings(deadline=None)
@given(
    presented=st.one_of(st.none(), st.just(""), unicode_values),
    path=wire_paths(),
    query=queries,
    method=st.sampled_from(METHODS),
)
def test_app_without_a_token_admits_everything(
    presented: str | None, path: str, query: str, method: str
) -> None:
    client = TestClient(minimal_app(None))
    headers = wire_headers(presented)
    response = client.request(method, url_for(path, query), headers=headers)
    assert response.status_code == 200


@settings(deadline=None)
@given(token=ascii_tokens, query=queries, method=st.sampled_from(METHODS))
def test_app_header_name_is_case_insensitive(token: str, query: str, method: str) -> None:
    client = TestClient(minimal_app(token))
    for name in ("X-Cairn-Proxy-Token", "x-cairn-proxy-token", "X-CAIRN-PROXY-TOKEN"):
        response = client.request(
            method, url_for("/readyz", query), headers={name.encode(): token.encode()}
        )
        assert response.status_code == 200


# --- the real wiring: build_app ---------------------------------------------------------

PASSWORD = "s3cr3t-pw-do-not-print"
WIRED_PATHS: Final = ("/healthz", "/healthz/x", "/Healthz", "/readyz", "/nope", "/")


def wired_app(token: str | None) -> FastAPI:
    # The token is passed explicitly, None included, so a token in a local .env cannot leak in.
    config = ApiSettings.model_validate(
        {"CAIRN_APP_DB_PASSWORD": PASSWORD, "CAIRN_PROXY_TOKEN": token}
    )

    async def probe() -> ProbeResult:
        return DbProbe(
            server_version_num=180001,
            extensions={"vector": "0.8.1", "btree_gist": "1.8"},
            role_name="cairn_app",
            role_is_superuser=False,
            role_bypasses_rls=False,
            schema_version=1,
            latency_ms=1.0,
        )

    return build_app(config, probe=probe, expected_head=1)


@settings(deadline=None, max_examples=40)
@given(
    data=token_and_value(ascii_tokens),
    path=st.sampled_from(WIRED_PATHS),
    method=st.sampled_from(("GET", "POST")),
)
def test_build_app_gate_matches_the_property(
    data: tuple[str, str | None], path: str, method: str
) -> None:
    token, presented = data
    client = TestClient(wired_app(token))
    headers = wire_headers(presented)
    response = client.request(method, "http://testserver" + path, headers=headers)

    refused = response.status_code == 404 and response.json() == REFUSAL
    if expected_admitted(path, presented, token):
        # Admitted requests reach the router: its own answers differ from the refusal.
        assert not (refused and response.headers.get("cache-control") == "no-store")
        if path == "/healthz" and method == "GET":
            assert response.status_code == 200
    else:
        assert refused
        assert response.headers["cache-control"] == "no-store"


@settings(deadline=None, max_examples=20)
@given(path=st.sampled_from(WIRED_PATHS), presented=st.one_of(st.none(), unicode_values))
def test_build_app_without_a_token_never_refuses(path: str, presented: str | None) -> None:
    client = TestClient(wired_app(None))
    headers = wire_headers(presented)
    response = client.get("http://testserver" + path, headers=headers)
    assert response.json() != REFUSAL
