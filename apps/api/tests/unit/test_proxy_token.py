"""Unit tests for the proxy token check (design §8, FT-16; Requirement 9).

No database is needed: the readiness probe is injected. The property tests for the gate and
for token hygiene (Properties 10 and 11) are separate tasks (5.5 and 5.6).
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import cairn_api.proxy_token as proxy_token
from cairn_api.main import build_app
from cairn_api.proxy_token import EXEMPT_PATHS, HEADER, allowed
from cairn_core.config import ApiSettings, CombinedSettings, env_keys, load_settings
from cairn_core.health import DbProbe, ProbeResult
from cairn_core.telemetry import JsonFormatter

TOKEN = "t" * 16 + "0123456789abcdef"  # 32 characters
assert len(TOKEN) == 32
PASSWORD = "s3cr3t-pw-do-not-print"


def settings_with(token: str | None) -> ApiSettings:
    # None is passed explicitly, so a token in a local .env cannot leak into the test.
    return ApiSettings.model_validate(
        {"CAIRN_APP_DB_PASSWORD": PASSWORD, "CAIRN_PROXY_TOKEN": token}
    )


def ready_probe() -> DbProbe:
    return DbProbe(
        server_version_num=180001,
        extensions={"vector": "0.8.1", "btree_gist": "1.8"},
        role_name="cairn_app",
        role_is_superuser=False,
        role_bypasses_rls=False,
        schema_version=1,
        latency_ms=1.0,
    )


def app_with(token: str | None) -> FastAPI:
    async def probe() -> ProbeResult:
        return ready_probe()

    return build_app(settings_with(token), probe=probe, expected_head=1)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app_with(TOKEN)) as c:
        yield c


# --- allowed() ---------------------------------------------------------------------------


def test_exempt_paths_are_only_healthz() -> None:
    assert {"/healthz"} == set(EXEMPT_PATHS)
    assert HEADER == "x-cairn-proxy-token"


def test_allowed_when_no_token_is_configured() -> None:
    assert allowed("/readyz", None, None) is True
    assert allowed("/anything", "whatever", None) is True


def test_allowed_for_the_exempt_path_without_a_header() -> None:
    assert allowed("/healthz", None, SecretStr(TOKEN)) is True
    assert allowed("/healthz", "wrong", SecretStr(TOKEN)) is True


def test_allowed_only_for_the_exact_token_elsewhere() -> None:
    expected = SecretStr(TOKEN)
    assert allowed("/readyz", TOKEN, expected) is True
    assert allowed("/readyz", None, expected) is False
    assert allowed("/readyz", "", expected) is False
    assert allowed("/readyz", TOKEN[:-1], expected) is False
    assert allowed("/readyz", TOKEN + "x", expected) is False
    assert allowed("/readyz", TOKEN.upper(), expected) is False
    assert allowed("/healthz/", None, expected) is False  # only the exact path is exempt


def test_allowed_handles_non_ascii_presented_values() -> None:
    assert allowed("/readyz", "tökén-" + "é" * 30, SecretStr(TOKEN)) is False
    assert allowed("/readyz", "é" * 32, SecretStr("é" * 32)) is True


def test_allowed_compares_with_hmac_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return a == b

    monkeypatch.setattr(proxy_token.hmac, "compare_digest", spy)
    assert allowed("/readyz", TOKEN, SecretStr(TOKEN)) is True
    assert allowed("/readyz", "nope", SecretStr(TOKEN)) is False
    assert calls == [(TOKEN.encode(), TOKEN.encode()), (b"nope", TOKEN.encode())]


def test_allowed_does_not_compare_when_the_check_is_off_or_exempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(a: bytes, b: bytes) -> bool:
        raise AssertionError("compare_digest must not run")

    monkeypatch.setattr(proxy_token.hmac, "compare_digest", boom)
    assert allowed("/readyz", None, None) is True
    assert allowed("/healthz", None, SecretStr(TOKEN)) is True
    assert allowed("/readyz", None, SecretStr(TOKEN)) is False  # no header, nothing to compare


# --- the middleware ----------------------------------------------------------------------


def test_healthz_is_exempt(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/readyz", "/", "/nope", "/healthz/", "/docs", "/openapi.json"])
def test_missing_header_gets_404_not_found(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}
    assert response.headers["cache-control"] == "no-store"


def test_wrong_header_gets_the_same_404(client: TestClient) -> None:
    missing = client.get("/readyz")
    wrong = client.get("/readyz", headers={HEADER: "x" * 32})
    assert wrong.status_code == 404
    assert (wrong.content, dict(wrong.headers)) == (missing.content, dict(missing.headers))


def test_refusal_covers_every_method(client: TestClient) -> None:
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
        response = client.request(method, "/readyz")
        assert response.status_code == 404, method
        assert response.json() == {"error": "not_found"}


def test_right_header_passes_to_the_route(client: TestClient) -> None:
    response = client.get("/readyz", headers={HEADER: TOKEN})
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_header_name_is_case_insensitive(client: TestClient) -> None:
    assert client.get("/readyz", headers={"X-Cairn-Proxy-Token": TOKEN}).status_code == 200


def test_right_header_still_gets_the_routers_own_404(client: TestClient) -> None:
    response = client.get("/nope", headers={HEADER: TOKEN})
    assert response.status_code == 404
    assert response.json() != {"error": "not_found"}  # FastAPI's {"detail": "Not Found"}


def test_no_token_configured_leaves_everything_open() -> None:
    with TestClient(app_with(None)) as c:
        assert c.get("/readyz").status_code == 200
        assert c.get("/healthz").status_code == 200


def test_refused_requests_are_still_access_logged_and_nothing_leaks() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("cairn-api"))
    logger = logging.getLogger("cairn.api.access")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    presented = "presented-" + "p" * 30
    try:
        with TestClient(app_with(TOKEN)) as c:
            refused = c.get("/readyz", headers={HEADER: presented})
            passed = c.get("/readyz", headers={HEADER: TOKEN})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    logged = stream.getvalue()
    assert '"status": 404' in logged or '"status":404' in logged
    for secret in (TOKEN, presented):
        assert secret not in logged
        assert secret not in refused.text
        assert secret not in passed.text
        assert secret not in str(refused.headers)


# --- settings ----------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in env_keys(CombinedSettings):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", PASSWORD + "-dbos")


def test_token_is_optional_and_unset_means_off(clean_env: None) -> None:
    assert load_settings(ApiSettings).proxy_token is None


def test_empty_token_counts_as_unset(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_PROXY_TOKEN", "")
    assert load_settings(ApiSettings).proxy_token is None


def test_a_32_character_token_loads_and_is_masked(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_PROXY_TOKEN", TOKEN)
    settings = load_settings(ApiSettings)
    assert settings.proxy_token is not None
    assert settings.proxy_token.get_secret_value() == TOKEN
    assert TOKEN not in repr(settings)
    assert TOKEN not in settings.model_dump_json()


def test_a_short_token_exits_2_naming_the_key_but_not_the_value(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    short = "s" * 31
    monkeypatch.setenv("CAIRN_PROXY_TOKEN", short)
    with pytest.raises(SystemExit) as exit_info:
        load_settings(ApiSettings)
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "CAIRN_PROXY_TOKEN" in err
    assert short not in err


def test_combined_settings_inherit_the_token_and_its_minimum(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CAIRN_PROXY_TOKEN", TOKEN)
    combined = load_settings(CombinedSettings)
    assert combined.proxy_token is not None
    assert combined.proxy_token.get_secret_value() == TOKEN

    monkeypatch.setenv("CAIRN_PROXY_TOKEN", "short")
    with pytest.raises(SystemExit) as exit_info:
        load_settings(CombinedSettings)
    assert exit_info.value.code == 2
    assert "CAIRN_PROXY_TOKEN" in capsys.readouterr().err


def test_combined_app_gets_the_check_through_build_app() -> None:
    settings = CombinedSettings.model_validate(
        {
            "CAIRN_APP_DB_PASSWORD": PASSWORD,
            "CAIRN_DBOS_DB_PASSWORD": PASSWORD + "-dbos",
            "CAIRN_PROXY_TOKEN": TOKEN,
        }
    )

    async def probe() -> ProbeResult:
        return ready_probe()

    with TestClient(build_app(settings, probe=probe, expected_head=1)) as c:
        assert c.get("/readyz").status_code == 404
        assert c.get("/readyz", headers={HEADER: TOKEN}).status_code == 200
        assert c.get("/healthz").status_code == 200
