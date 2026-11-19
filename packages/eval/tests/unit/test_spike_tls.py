"""The S1 and S2 spike connections carry the TLS settings and the admin settings
(free-tier FT-14, design §6, Requirement 4.4). No database is needed."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cairn_core.config import AdminSettings, env_keys
from cairn_core.testing import AdminTarget, DbTestConfig
from cairn_eval.spikes.s1_acl_corpus.cli import spike_owner_dsn
from cairn_eval.spikes.s2_rls_explain import harness

ROLE_SECRETS = {
    "CAIRN_OWNER_DB_PASSWORD": "owner-pw-FAKE-1",
    "CAIRN_APP_DB_PASSWORD": "app-pw-FAKE-1",
    "CAIRN_DBOS_DB_PASSWORD": "dbos-pw-FAKE-1",
}
VERIFY = {"CAIRN_DB_SSLMODE": "verify-full", "CAIRN_DB_SSLROOTCERT": "/certs/root.crt"}
VERIFY_TLS = {"sslmode": "verify-full", "sslrootcert": "/certs/root.crt"}


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings read the process environment and ``.env`` even through ``model_validate``."""
    for key in env_keys(AdminSettings) | env_keys(AdminTarget):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def tls_of(params: dict[str, Any]) -> dict[str, Any]:
    return {k: params[k] for k in ("sslmode", "sslrootcert") if k in params}


def admin_settings(**extra: str) -> AdminSettings:
    return AdminSettings.model_validate(ROLE_SECRETS | extra)


def test_s1_spike_dsn_carries_the_tls_settings() -> None:
    target = AdminTarget.model_validate({"POSTGRES_PASSWORD": "super-pw-FAKE-1"})
    cfg = DbTestConfig(target=target, settings=admin_settings(**VERIFY))
    params = conninfo_to_dict(spike_owner_dsn(cfg, "cairn_spike_s1"))
    assert tls_of(params) == VERIFY_TLS
    assert (params["dbname"], params["user"]) == ("cairn_spike_s1", "cairn_owner")


def test_s2_opens_its_admin_connection_with_the_admin_settings_and_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_connect(*args: Any, **kwargs: Any) -> str:
        captured.append(kwargs)
        return "connection"

    monkeypatch.setattr(harness.psycopg, "connect", fake_connect)
    settings = admin_settings(
        CAIRN_DB_ADMIN_USER="cairn_admin",
        CAIRN_DB_ADMIN_DATABASE="admin_db",
        CAIRN_DB_ADMIN_PASSWORD="admin-pw-FAKE-1",
        **VERIFY,
    )
    harness._open_admin(settings, None)
    harness._open_admin(settings, "someone_else")
    first, second = captured
    assert tls_of(first) == VERIFY_TLS
    assert (first["user"], first["dbname"], first["password"]) == (
        "cairn_admin",
        "admin_db",
        "admin-pw-FAKE-1",
    )
    assert second["user"] == "someone_else"


def test_s2_admin_connection_needs_an_admin_password() -> None:
    with pytest.raises(harness.HarnessError, match="CAIRN_DB_ADMIN_PASSWORD or POSTGRES_PASSWORD"):
        harness._open_admin(admin_settings(), None)


class FakeInfo:
    host = "127.0.0.1"
    port = 55432

    def get_parameters(self) -> dict[str, str]:
        return {"dbname": "postgres", "sslmode": "verify-full", "sslrootcert": "/certs/root.crt"}


class FakeAdminConn:
    info = FakeInfo()


def test_s2_role_connections_copy_the_admin_connection_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_connect(conninfo: str, **_kwargs: Any) -> str:
        captured.append(conninfo)
        return "connection"

    monkeypatch.setattr(harness.psycopg, "connect", fake_connect)
    admin = cast("psycopg.Connection[Any]", FakeAdminConn())
    harness._connect(admin, "cairn_spike_s2", "cairn_app", "app-pw-FAKE-1")
    (dsn,) = captured
    params = conninfo_to_dict(dsn)
    assert tls_of(params) == VERIFY_TLS
    assert (params["dbname"], params["user"], params["port"]) == (
        "cairn_spike_s2",
        "cairn_app",
        "55432",
    )
