"""Which database the test fixtures aim at (free-tier design §9 and §15, Requirements 6.4 to 6.6).

``CAIRN_TEST_ADMIN_MODE`` unset keeps the compose ``db`` and its superuser. ``managed``
selects ``ft-db``: ``CAIRN_FREETIER_DB_PORT``, user ``cairn_admin`` and
``CAIRN_MANAGED_ADMIN_PASSWORD``. No database is needed: ``psycopg.connect`` is replaced by a
recorder and ``drop_database`` gets a connection that records the statements it is sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from cairn_core import testing
from cairn_core.config import AdminSettings, env_keys
from cairn_core.testing import (
    AdminTarget,
    DatabaseUnavailable,
    DbTestConfig,
    FreshDb,
    connect_admin,
    drop_database,
    load_config,
)

SUPER_PW = "super-pw-FAKE-1"
MANAGED_PW = "managed-pw-FAKE-1"
ROLE_SECRETS = {
    "CAIRN_OWNER_DB_PASSWORD": "owner-pw-FAKE-1",
    "CAIRN_APP_DB_PASSWORD": "app-pw-FAKE-1",
    "CAIRN_DBOS_DB_PASSWORD": "dbos-pw-FAKE-1",
}
ALL_KEYS = {"CAIRN_TEST_ADMIN_MODE", "CAIRN_TEST_ADMIN_PORT"} | env_keys(AdminTarget)


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings read the process environment and ``.env``: start from neither."""
    for key in ALL_KEYS | env_keys(AdminSettings):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    for key, value in ROLE_SECRETS.items():
        monkeypatch.setenv(key, value)


def target(**values: str | int) -> AdminTarget:
    return AdminTarget.model_validate(values)


# --- target selection -----------------------------------------------------------------


def test_default_mode_is_the_superuser_on_the_compose_port() -> None:
    t = target(POSTGRES_PASSWORD=SUPER_PW, CAIRN_TEST_ADMIN_PORT=55432)
    assert t.mode == "superuser"
    assert (t.host, t.port, t.user) == ("127.0.0.1", 55432, "postgres")
    assert t.password.get_secret_value() == SUPER_PW


def test_managed_mode_selects_the_freetier_port_the_managed_user_and_its_password() -> None:
    t = target(
        CAIRN_TEST_ADMIN_MODE="managed",
        CAIRN_FREETIER_DB_PORT=55433,
        CAIRN_MANAGED_ADMIN_PASSWORD=MANAGED_PW,
        # keys of the other mode are present and must not be used
        CAIRN_TEST_ADMIN_PORT=55432,
        CAIRN_TEST_ADMIN_USER="somebody_else",
        POSTGRES_PASSWORD=SUPER_PW,
    )
    assert (t.mode, t.port, t.user) == ("managed", 55433, "cairn_admin")
    assert t.password.get_secret_value() == MANAGED_PW


def test_managed_mode_needs_no_superuser_password_and_defaults_to_port_55433() -> None:
    t = target(CAIRN_TEST_ADMIN_MODE="managed", CAIRN_MANAGED_ADMIN_PASSWORD=MANAGED_PW)
    assert t.port == 55433


def test_the_host_applies_to_both_modes() -> None:
    t = target(
        CAIRN_TEST_ADMIN_MODE="managed",
        CAIRN_MANAGED_ADMIN_PASSWORD=MANAGED_PW,
        CAIRN_TEST_ADMIN_HOST="10.0.0.9",
    )
    assert t.host == "10.0.0.9"


def test_repr_hides_both_passwords() -> None:
    t = target(POSTGRES_PASSWORD=SUPER_PW, CAIRN_MANAGED_ADMIN_PASSWORD=MANAGED_PW)
    assert SUPER_PW not in repr(t)
    assert MANAGED_PW not in repr(t)


def test_load_config_reads_the_mode_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_TEST_ADMIN_MODE", "managed")
    monkeypatch.setenv("CAIRN_MANAGED_ADMIN_PASSWORD", MANAGED_PW)
    monkeypatch.setenv("CAIRN_FREETIER_DB_PORT", "55444")
    config = load_config()
    assert (config.target.mode, config.target.port, config.target.user) == (
        "managed",
        55444,
        "cairn_admin",
    )


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"CAIRN_TEST_ADMIN_MODE": "managed", "POSTGRES_PASSWORD": SUPER_PW}, "MANAGED_ADMIN"),
        ({"CAIRN_MANAGED_ADMIN_PASSWORD": MANAGED_PW}, "POSTGRES_PASSWORD"),
        ({"CAIRN_TEST_ADMIN_MODE": "bogus", "POSTGRES_PASSWORD": SUPER_PW}, "ADMIN_MODE"),
    ],
)
def test_incomplete_or_invalid_target_settings_name_the_key_and_no_value(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], named: str
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(DatabaseUnavailable) as raised:
        load_config()
    message = str(raised.value)
    assert named in message
    for secret in (SUPER_PW, MANAGED_PW, "bogus"):
        assert secret not in message


# --- connections ----------------------------------------------------------------------


def config_for(**values: str | int) -> DbTestConfig:
    return DbTestConfig(
        target=target(**values), settings=AdminSettings.model_validate(ROLE_SECRETS)
    )


def test_connect_admin_in_managed_mode_uses_the_managed_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_connect(**kwargs: Any) -> str:
        captured.append(kwargs)
        return "connection"

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    connect_admin(
        config_for(
            CAIRN_TEST_ADMIN_MODE="managed",
            CAIRN_MANAGED_ADMIN_PASSWORD=MANAGED_PW,
            CAIRN_FREETIER_DB_PORT=55433,
        )
    )
    (call,) = captured
    assert (call["port"], call["user"], call["password"]) == (55433, "cairn_admin", MANAGED_PW)


def test_an_unreachable_managed_database_says_how_to_start_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**_: Any) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refuse)
    with pytest.raises(DatabaseUnavailable, match=r"ft-db.*55433.*make freetier-up") as raised:
        connect_admin(
            config_for(CAIRN_TEST_ADMIN_MODE="managed", CAIRN_MANAGED_ADMIN_PASSWORD=MANAGED_PW)
        )
    assert MANAGED_PW not in str(raised.value)


def test_fresh_database_admin_dsn_uses_the_selected_login() -> None:
    cfg = config_for(CAIRN_TEST_ADMIN_MODE="managed", CAIRN_MANAGED_ADMIN_PASSWORD=MANAGED_PW)
    dsn = testing._dsn(cfg, "cairn_test_00000000", cfg.target.user, MANAGED_PW)
    assert "user=cairn_admin" in dsn
    assert "port=55433" in dsn


def test_connect_superuser_is_an_alias_of_connect_admin() -> None:
    assert FreshDb.connect_superuser is FreshDb.connect_admin


# --- drop_database --------------------------------------------------------------------


class RecordingConnection:
    """Records each statement as text; answers the owner lookup with ``owner``."""

    def __init__(
        self, owner: str | None = "cairn_owner", fail_on: str | None = None, superuser: bool = False
    ) -> None:
        self.statements: list[str] = []
        self.owner = owner
        self.fail_on = fail_on
        self.superuser = superuser

    def execute(self, query: str | sql.Composable, params: Any = None) -> RecordingConnection:
        text = query.as_string() if isinstance(query, sql.Composable) else query
        self.statements.append(text)
        if self.fail_on and text.startswith(self.fail_on):
            raise psycopg.errors.InsufficientPrivilege("denied")
        self._row: tuple[Any, ...] | None = None
        if "pg_get_userbyid" in text:
            self._row = None if self.owner is None else (self.owner,)
        elif "rolsuper" in text:
            self._row = ("cairn_admin", self.superuser, True, True, True)
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


DROP = 'DROP DATABASE IF EXISTS "cairn_test_ab12cd34" WITH (FORCE)'


@pytest.mark.parametrize("managed", [True, False, None])
@pytest.mark.parametrize("name", ["cairn", "cairn_dbos", "postgres", "cairn_testing", ""])
def test_drop_database_refuses_names_outside_the_test_prefix(
    name: str, managed: bool | None
) -> None:
    conn = RecordingConnection()
    with pytest.raises(ValueError, match="refusing to drop"):
        drop_database(conn, name, managed=managed)  # type: ignore[arg-type]
    assert conn.statements == []


def test_drop_database_as_a_superuser_drops_directly() -> None:
    conn = RecordingConnection()
    drop_database(conn, "cairn_test_ab12cd34", managed=False)  # type: ignore[arg-type]
    assert conn.statements == [DROP]


def test_drop_database_in_managed_mode_sets_the_owner_role_and_resets_it() -> None:
    conn = RecordingConnection(owner="cairn_dbos")
    drop_database(conn, "cairn_test_ab12cd34", managed=True)  # type: ignore[arg-type]
    assert conn.statements[1:] == ['SET ROLE "cairn_dbos"', DROP, "RESET ROLE"]


def test_drop_database_resets_the_role_when_the_drop_fails() -> None:
    conn = RecordingConnection(fail_on="DROP")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        drop_database(conn, "cairn_test_ab12cd34", managed=True)  # type: ignore[arg-type]
    assert conn.statements[-1] == "RESET ROLE"


def test_drop_database_in_managed_mode_does_nothing_for_a_missing_database() -> None:
    conn = RecordingConnection(owner=None)
    drop_database(conn, "cairn_test_ab12cd34", managed=True)  # type: ignore[arg-type]
    assert not any(s.startswith(("SET ROLE", "DROP")) for s in conn.statements)


@pytest.mark.parametrize(("superuser", "set_role"), [(True, False), (False, True)])
def test_drop_database_detects_the_mode_from_the_admin_role(
    superuser: bool, set_role: bool
) -> None:
    conn = RecordingConnection(superuser=superuser)
    drop_database(conn, "cairn_test_ab12cd34")  # type: ignore[arg-type]
    assert any(s.startswith("SET ROLE") for s in conn.statements) is set_role
    assert DROP in conn.statements
