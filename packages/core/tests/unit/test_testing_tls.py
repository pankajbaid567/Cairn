"""The test fixtures' connections carry the TLS settings and the admin database
(free-tier FT-14, Requirement 4.4). No database is needed: ``psycopg.connect`` is replaced
by a recorder, and DSNs are parsed back with ``conninfo_to_dict``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cairn_core import testing
from cairn_core.config import AdminSettings, env_keys
from cairn_core.testing import AdminTarget, DbTestConfig, connect_admin

ROLE_SECRETS = {
    "CAIRN_OWNER_DB_PASSWORD": "owner-pw-FAKE-1",
    "CAIRN_APP_DB_PASSWORD": "app-pw-FAKE-1",
    "CAIRN_DBOS_DB_PASSWORD": "dbos-pw-FAKE-1",
}


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings read the process environment and ``.env`` even through ``model_validate``."""
    for key in env_keys(AdminSettings) | env_keys(AdminTarget):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def config(**settings: str) -> DbTestConfig:
    target = AdminTarget.model_validate(
        {"CAIRN_TEST_ADMIN_PORT": 55432, "POSTGRES_PASSWORD": "super-pw-FAKE-1"}
    )
    return DbTestConfig(
        target=target, settings=AdminSettings.model_validate(ROLE_SECRETS | settings)
    )


def tls_of(params: dict[str, Any]) -> dict[str, Any]:
    return {k: params[k] for k in ("sslmode", "sslrootcert") if k in params}


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, {"sslmode": "prefer"}),
        (
            {"CAIRN_DB_SSLMODE": "verify-full", "CAIRN_DB_SSLROOTCERT": "/certs/root.crt"},
            {"sslmode": "verify-full", "sslrootcert": "/certs/root.crt"},
        ),
    ],
)
def test_connect_admin_passes_tls_and_starts_in_the_admin_database(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str], expected: dict[str, str]
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_connect(**kwargs: Any) -> str:
        captured.append(kwargs)
        return "connection"

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    connect_admin(config(CAIRN_DB_ADMIN_DATABASE="admin_db", **settings))
    connect_admin(config(**settings), "other_db")
    first, second = captured
    assert tls_of(first) == expected
    assert tls_of(second) == expected
    assert (first["dbname"], second["dbname"]) == ("admin_db", "other_db")
    assert first["port"] == 55432


def test_fresh_database_dsns_carry_the_tls_settings() -> None:
    cfg = config(CAIRN_DB_SSLMODE="require", CAIRN_DB_SSLROOTCERT="/certs/root.crt")
    dsn = testing._dsn(cfg, "cairn_test_00000000", "cairn_app", "app-pw-FAKE-1")
    params = conninfo_to_dict(dsn)
    assert tls_of(params) == {"sslmode": "require", "sslrootcert": "/certs/root.crt"}
    assert (params["dbname"], params["user"]) == ("cairn_test_00000000", "cairn_app")
