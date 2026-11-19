"""The environment-driven path to the Phase 1 routes (a startup crash here once slipped past tests
that injected the runtime directly, and was caught by the free-tier gate)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn_api.main import build_app, maybe_search_runtime
from cairn_core.config import ApiSettings

PHASE1 = {
    "CAIRN_SESSION_SECRET": "s" * 40,
    "CAIRN_AUDIT_HASH_KEY": "a" * 40,
    "CAIRN_EMBEDDER": "fake",
    "CAIRN_RERANKER": "fake",
    "CAIRN_ANSWER_PROVIDER": "fake",
    "CAIRN_JUDGE_PROVIDER": "fake",
}


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.chdir(str(tmp_path))
    monkeypatch.delenv("CAIRN_SESSION_SECRET", raising=False)


def api() -> ApiSettings:
    return ApiSettings.model_validate({"CAIRN_APP_DB_PASSWORD": "x", "CAIRN_PROXY_TOKEN": None})


def test_without_a_session_secret_the_app_serves_only_the_probes() -> None:
    assert maybe_search_runtime(api()) is None


def test_with_the_phase_1_keys_a_runtime_is_built_and_its_routes_are_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in PHASE1.items():
        monkeypatch.setenv(key, value)
    runtime = maybe_search_runtime(api())
    assert runtime is not None
    assert runtime.providers.embedder.model_key == "fake-embed-v1"
    app = build_app(api(), probe=None, search=runtime)
    client = TestClient(
        app
    )  # no lifespan: no database is opened, and these refuse before any query
    assert client.get("/me").status_code == 401  # the route exists and wants a session
    assert client.post("/search", json={"query": "x"}).status_code == 401
    assert client.post("/answer", json={"question": "x"}).status_code == 401
    assert client.post("/auth/logout").status_code == 204
    assert client.get("/healthz").status_code == 200
