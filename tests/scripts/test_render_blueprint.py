"""The Render Blueprint `render.yaml` (free-tier retarget, task 9.1).

Requirements 6.2, 17.1, 17.2 and 17.11: one Docker web service on the free plan with the
`/healthz` health check, started with `cairn-combined` from the shared Dockerfile; every
secret-like environment variable is `sync: false` without a value; no admin, owner or
superuser password key; and TLS settings that match `.env.neon.example` (FV11).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BLUEPRINT: Final = REPO_ROOT / "render.yaml"
SHARED_DOCKERFILE: Final = "infra/docker/python-app.Dockerfile"
SECRET_LIKE: Final = re.compile(r"PASSWORD|SECRET|TOKEN|KEY", re.IGNORECASE)
FORBIDDEN_KEY: Final = re.compile(r"OWNER|ADMIN|SUPERUSER|POSTGRES|NEON", re.IGNORECASE)
# Secrets this service is expected to ask for, so a rename or a dropped key is noticed.
EXPECTED_SECRET_KEYS: Final = {
    "CAIRN_APP_DB_PASSWORD",
    "CAIRN_DBOS_DB_PASSWORD",
    "CAIRN_PROXY_TOKEN",
    "CLOUDFLARE_API_TOKEN",
}


@pytest.fixture(scope="module")
def blueprint() -> dict[str, Any]:
    loaded = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def service(blueprint: dict[str, Any]) -> dict[str, Any]:
    services = blueprint["services"]
    assert isinstance(services, list)
    assert len(services) == 1, "the Blueprint declares exactly one service"
    only = services[0]
    assert isinstance(only, dict)
    return only


@pytest.fixture(scope="module")
def env_vars(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = service["envVars"]
    assert isinstance(entries, list)
    keyed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        assert isinstance(entry, dict)
        key = entry["key"]
        assert key not in keyed, f"duplicate environment variable {key}"
        keyed[key] = entry
    return keyed


def test_blueprint_declares_only_one_web_service(blueprint: dict[str, Any]) -> None:
    assert set(blueprint) == {"services"}, "no databases, groups or other top-level entries"


def test_service_is_a_free_docker_web_service(service: dict[str, Any]) -> None:
    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    assert service["plan"] == "free"
    assert service["region"] == "singapore"


def test_service_runs_cairn_combined_from_the_shared_dockerfile(service: dict[str, Any]) -> None:
    assert service["dockerfilePath"] == f"./{SHARED_DOCKERFILE}"
    assert service["dockerContext"] == "."
    assert service["dockerCommand"] == "cairn-combined"
    assert (REPO_ROOT / SHARED_DOCKERFILE).is_file()


def test_health_check_is_process_liveness_only(service: dict[str, Any]) -> None:
    assert service["healthCheckPath"] == "/healthz"


def test_auto_deploy_is_off_and_not_a_yaml_boolean(service: dict[str, Any]) -> None:
    # YAML 1.1 reads an unquoted `off` as False; Render needs the string "off".
    assert service["autoDeployTrigger"] == "off"


def test_render_blueprint_has_no_secret_values(env_vars: dict[str, dict[str, Any]]) -> None:
    secret_keys = {key for key in env_vars if SECRET_LIKE.search(key)}
    assert secret_keys >= EXPECTED_SECRET_KEYS
    for key in secret_keys:
        entry = env_vars[key]
        assert entry.get("sync") is False, f"{key} must be sync: false"
        assert set(entry) == {"key", "sync"}, f"{key} must carry no value"


def test_every_entry_has_a_value_or_is_asked_for(env_vars: dict[str, dict[str, Any]]) -> None:
    for key, entry in env_vars.items():
        has_value = "value" in entry
        asked = entry.get("sync") is False
        assert has_value != asked, f"{key} needs exactly one of a value or sync: false"
        assert set(entry) <= {"key", "value", "sync"}, f"{key} uses an unexpected field"
        if has_value:
            assert isinstance(entry["value"], str), f"{key} value must be a string"


def test_no_admin_owner_or_superuser_credential_key(env_vars: dict[str, dict[str, Any]]) -> None:
    offending = sorted(key for key in env_vars if FORBIDDEN_KEY.search(key))
    assert offending == []


def test_tls_settings_match_the_neon_template(env_vars: dict[str, dict[str, Any]]) -> None:
    neon = (REPO_ROOT / ".env.neon.example").read_text(encoding="utf-8")
    assert env_vars["CAIRN_DB_SSLMODE"]["value"] == "verify-full"
    root_cert = env_vars["CAIRN_DB_SSLROOTCERT"]["value"]
    assert root_cert == "/etc/ssl/certs/ca-certificates.crt", "FV11: not `system`"
    assert f"CAIRN_DB_SSLROOTCERT={root_cert}" in neon
    assert "CAIRN_DB_SSLMODE=verify-full" in neon


def test_runtime_settings(env_vars: dict[str, dict[str, Any]]) -> None:
    assert env_vars["CAIRN_ENV"]["value"] == "demo"
    assert env_vars["CAIRN_DB_NAME"]["value"] == "cairn"
    assert env_vars["CAIRN_DB_PORT"]["value"] == "5432"
    assert env_vars["CAIRN_HEARTBEAT_CRON"]["value"] == "0 */5 * * * *"
    assert env_vars["CAIRN_DB_HOST"] == {"key": "CAIRN_DB_HOST", "sync": False}
    # PORT is left to Render (default 10000, which is also the process default).
    assert "PORT" not in env_vars
    # Build arguments come from environment variables (FV6): never shadow the Dockerfile's.
    assert "PACKAGE" not in env_vars


def test_blueprint_text_holds_no_credential_shaped_literal() -> None:
    text = BLUEPRINT.read_text(encoding="utf-8")
    assert not re.search(r"postgres(?:ql)?://[^\s:@]+:[^\s@]+@", text), "no connection string"
    assert not re.search(r"\b[0-9a-f]{40,}\b", text, re.IGNORECASE), "no long hex or token"
