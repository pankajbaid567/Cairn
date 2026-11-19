"""Property tests for proxy token hygiene (task 5.6).

Feature: cairn-free-tier-retarget, Property 11: Proxy token values never reach logs or bodies
**Validates: Requirements 9.4**

For any configured token (at least 32 characters, carrying a distinctive marker) and any
presented ``X-Cairn-Proxy-Token`` value (absent, equal, a prefix, an extension, a different
case, or another marker-bearing string), after a request to any path with any query string
(the query may itself carry the token or the presented value), none of these contains the token
or the presented value:

* any log record emitted anywhere (all loggers are lowered to DEBUG and captured; the message,
  the fields added through ``extra``, ``args`` and the output of both the plain and the JSON
  formatter are searched),
* the response body,
* the response headers,
* ``repr``, ``str``, ``model_dump`` and ``model_dump_json`` of the ``ApiSettings`` holding it.

Refused and accepted requests are both covered. Each secret is also searched for in its JSON,
``repr`` and URL-encoded spellings. Paths are generated without the marker: a path is logged on
purpose, and it is not a secret.

No database is needed: the readiness probe is injected. Settings pass ``CAIRN_PROXY_TOKEN``
explicitly, because a local ``.env`` may hold a token (task 5.2 notes).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple
from urllib.parse import quote, quote_plus

from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_api.main import build_app
from cairn_api.proxy_token import HEADER
from cairn_core.config import ApiSettings
from cairn_core.health import DbProbe, ProbeResult
from cairn_core.telemetry import JsonFormatter

MARKER = "cairn-tok-ZQX7-"
PASSWORD = "s3cr3t-pw-do-not-print"

# Printable ASCII without the space: valid in a header value, and it includes the characters
# that JSON, repr and URL encoding spell differently (quote, backslash, percent, plus, ampersand).
VALUE_ALPHABET = st.characters(min_codepoint=0x21, max_codepoint=0x7E)

SEGMENTS = st.from_regex(r"[a-z0-9_-]{1,8}", fullmatch=True)
KNOWN_PATHS = ["/healthz", "/healthz/", "/readyz", "/", "/nope", "/docs", "/openapi.json"]
PATHS = st.one_of(
    st.sampled_from(KNOWN_PATHS),
    st.lists(SEGMENTS, min_size=1, max_size=4).map(lambda parts: "/" + "/".join(parts)),
)
METHODS = st.sampled_from(["GET", "POST", "HEAD", "DELETE", "OPTIONS"])


class Scenario(NamedTuple):
    token: str
    presented: str | None
    path: str
    query: str
    method: str


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    token = MARKER + draw(st.text(VALUE_ALPHABET, min_size=17, max_size=60))
    assert len(token) >= 32
    kind = draw(st.sampled_from(["absent", "equal", "other", "prefix", "extended", "case"]))
    presented: str | None
    if kind == "absent":
        presented = None
    elif kind == "equal":
        presented = token
    elif kind == "other":
        presented = MARKER + draw(st.text(VALUE_ALPHABET, min_size=1, max_size=40))
    elif kind == "prefix":
        presented = token[: draw(st.integers(len(MARKER) + 1, len(token) - 1))]
    elif kind == "extended":
        presented = token + draw(st.text(VALUE_ALPHABET, min_size=1, max_size=10))
    else:
        presented = token.swapcase()  # the marker has letters, so this always differs

    query_part = draw(st.sampled_from(["none", "token", "presented", "random"]))
    query = ""
    if query_part == "token":
        query = "t=" + quote(token, safe="")
    elif query_part == "presented" and presented is not None:
        query = "p=" + quote(presented, safe="")
    elif query_part == "random":
        query = "q=" + quote(draw(st.text(min_size=1, max_size=30)), safe="")
    return Scenario(token, presented, draw(PATHS), query, draw(METHODS))


def variants(secret: str) -> set[str]:
    """The secret as written, and as it would appear in JSON, in a repr and in a URL."""
    return {
        secret,
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
        repr(secret)[1:-1],
        quote(secret, safe=""),
        quote_plus(secret),
    }


def assert_absent(secrets: list[str], where: str, *texts: str) -> None:
    for secret in secrets:
        for variant in variants(secret):
            for text in texts:
                assert variant not in text, f"{variant!r} found in {where}"


# --- log capture ---------------------------------------------------------------------------


CLIENT_LOGGERS = frozenset({"httpx", "httpx2", "httpcore"})
"""Loggers of the HTTP client that drives the app in-process. They are the caller's side of the
request (the caller knows its own URL and query string), not the app's logs."""


class Capture(logging.Handler):
    """Keeps every record as text: message, extra fields, args, and both formatters' output."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.dumps: list[str] = []
        self.loggers: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.split(".")[0] in CLIENT_LOGGERS:
            return  # the test client logs the URL it requests, query string included
        self.loggers.append(record.name)
        self.dumps.append(
            "\n".join(
                [
                    record.getMessage(),
                    repr(vars(record)),  # includes msg, args and everything given as extra
                    logging.Formatter().format(record),
                    JsonFormatter("cairn-api").format(record),
                ]
            )
        )


@contextmanager
def capture_all_logs() -> Iterator[Capture]:
    """Lower every logger to DEBUG and collect what any of them emits."""
    capture = Capture()
    root = logging.getLogger()
    named = [
        lg for lg in logging.root.manager.loggerDict.values() if isinstance(lg, logging.Logger)
    ]
    saved_levels = [(lg, lg.level) for lg in [root, *named]]
    saved_disabled = [(lg, lg.disabled) for lg in named]
    detached = [lg for lg in named if not lg.propagate]
    root.addHandler(capture)
    for lg in detached:
        lg.addHandler(capture)
    for lg in named:
        lg.setLevel(logging.NOTSET)
        lg.disabled = False
    root.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        root.removeHandler(capture)
        for lg in detached:
            lg.removeHandler(capture)
        for lg, level in saved_levels:
            lg.setLevel(level)
        for lg, disabled in saved_disabled:
            lg.disabled = disabled


# --- the app -------------------------------------------------------------------------------


def settings_with(token: str) -> ApiSettings:
    return ApiSettings.model_validate(
        {"CAIRN_APP_DB_PASSWORD": PASSWORD, "CAIRN_PROXY_TOKEN": token}
    )


def client_for(token: str) -> TestClient:
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

    return TestClient(build_app(settings_with(token), probe=probe, expected_head=1))


# --- Property 11 ---------------------------------------------------------------------------


@settings(deadline=None, max_examples=60)
@given(scenario=scenarios())
def test_token_and_presented_value_never_reach_logs_or_responses(scenario: Scenario) -> None:
    token, presented, path, query, method = scenario
    secrets = [token] if presented is None else [token, presented]
    headers = {} if presented is None else {HEADER: presented}
    url = f"{path}?{query}" if query else path

    client = client_for(token)
    with capture_all_logs() as logs:
        response = client.request(method, url, headers=headers)

    # The request was handled, and logged: the log check is not vacuous.
    assert "cairn.api.access" in logs.loggers
    refused = path != "/healthz" and presented != token
    if refused:
        assert response.status_code == 404
        if method != "HEAD":  # a HEAD response has no body
            assert response.json() == {"error": "not_found"}

    body = response.content.decode("utf-8", errors="replace")
    header_text = "\n".join(f"{k}: {v}" for k, v in response.headers.items())
    assert_absent(secrets, "a log record", *logs.dumps)
    assert_absent(secrets, "the response body", body)
    assert_absent(secrets, "the response headers", header_text)


@settings(deadline=None, max_examples=60)
@given(scenario=scenarios())
def test_token_never_appears_in_the_settings_repr_or_dump(scenario: Scenario) -> None:
    token, presented, *_ = scenario
    secrets = [token] if presented is None else [token, presented]
    configured = settings_with(token)
    assert configured.proxy_token is not None
    assert configured.proxy_token.get_secret_value() == token  # it is held, just not shown
    assert_absent(
        secrets,
        "the ApiSettings text forms",
        repr(configured),
        str(configured),
        repr(configured.proxy_token),
        str(configured.proxy_token),
        configured.model_dump_json(),
        repr(configured.model_dump()),
    )
