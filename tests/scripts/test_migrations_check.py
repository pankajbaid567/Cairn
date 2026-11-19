"""``scripts/migrations_check.py`` hands the admin connection's TLS settings to the
``cairn-db migrate`` subprocess (free-tier FT-14, Requirement 4.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn_core.config import AdminSettings, env_keys
from cairn_core.testing import AdminTarget, FreshDb
from scripts.migrations_check import cli_env

ROLE_SECRETS = {
    "CAIRN_OWNER_DB_PASSWORD": "owner-pw-FAKE-1",
    "CAIRN_APP_DB_PASSWORD": "app-pw-FAKE-1",
    "CAIRN_DBOS_DB_PASSWORD": "dbos-pw-FAKE-1",
}
DB = FreshDb(
    name="cairn_test_0a1b2c3d",
    host="127.0.0.1",
    port=55432,
    owner_dsn="",
    app_dsn="",
    admin_dsn="",
)


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings read the process environment and ``.env`` even through ``model_validate``."""
    for key in env_keys(AdminSettings) | env_keys(AdminTarget):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def test_cli_env_points_at_the_database_with_the_default_tls() -> None:
    env = cli_env({"PATH": "/bin"}, DB, AdminSettings.model_validate(ROLE_SECRETS))
    assert env["PATH"] == "/bin"
    assert (env["CAIRN_DB_NAME"], env["CAIRN_DB_HOST"], env["CAIRN_DB_PORT"]) == (
        "cairn_test_0a1b2c3d",
        "127.0.0.1",
        "55432",
    )
    assert env["CAIRN_DB_SSLMODE"] == "prefer"
    assert "CAIRN_DB_SSLROOTCERT" not in env


def test_cli_env_carries_the_root_certificate_when_set() -> None:
    settings = AdminSettings.model_validate(
        ROLE_SECRETS
        | {"CAIRN_DB_SSLMODE": "verify-full", "CAIRN_DB_SSLROOTCERT": "/certs/root.crt"}
    )
    env = cli_env({}, DB, settings)
    assert (env["CAIRN_DB_SSLMODE"], env["CAIRN_DB_SSLROOTCERT"]) == (
        "verify-full",
        "/certs/root.crt",
    )
