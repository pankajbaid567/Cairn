"""Property tests for admin mode resolution (free-tier design Property 5).

Feature: cairn-free-tier-retarget, Property 5: Admin mode resolution is decided before any change

**Validates: Requirements 5.1, 5.2, 5.3**

For every combination of admin facts (superuser, CREATEROLE, CREATEDB, BYPASSRLS) and requested
mode (``auto``, ``superuser``, ``managed``):

- ``resolve_mode`` returns the documented mode, or raises ``BootstrapError`` that names the
  requested mode or the missing attribute and none of the attributes the admin holds;
- ``bootstrap()`` on a fake cluster refuses in exactly those cases, and the fake cluster's log of
  executed statements is empty, so nothing changed before the decision. When the mode resolves,
  ``bootstrap()`` runs in that mode.

No database is needed. The fake cluster below is a reduced copy of the one in
``test_bootstrap.py``: it answers the catalog reads and records every other statement.
"""

from __future__ import annotations

import re
from typing import Any, cast, get_args

import psycopg
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from psycopg import sql

from cairn_core.bootstrap import AdminFacts as Facts
from cairn_core.bootstrap import BootstrapError, bootstrap, resolve_mode
from cairn_core.config import AdminModeSetting, AdminSettings

PBT = settings(deadline=None)

REQUESTED_MODES: tuple[AdminModeSetting, ...] = get_args(AdminModeSetting)

# Verifier-shaped value, so the bootstrap run does not need libpq or a connection.
VERIFIER = "SCRAM-SHA-256$4096:c2FsdHNhbHRzYWx0c2E=$c3RvcmVka2V5Cg==:c2VydmVya2V5Cg=="


def encrypt(_password: str, _role: str) -> str:
    return VERIFIER


# --- strategies --------------------------------------------------------------------------

admin_names = st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)
requested_modes = st.sampled_from(REQUESTED_MODES)


@st.composite
def admin_facts(draw: st.DrawFn) -> Facts:
    return Facts(
        user=draw(admin_names),
        is_superuser=draw(st.booleans()),
        can_create_role=draw(st.booleans()),
        can_create_db=draw(st.booleans()),
        bypasses_rls=draw(st.booleans()),
    )


# --- the expectation, written out from the design text -----------------------------------


def expected_mode(requested: AdminModeSetting, facts: Facts) -> str | None:
    """The documented result, or ``None`` when ``resolve_mode`` must raise."""
    if requested in ("superuser", "auto") and facts.is_superuser:
        return "superuser"
    if requested == "superuser":
        return None
    if facts.can_create_role and facts.can_create_db:
        return "managed"
    return None


def check_refusal(message: str, requested: AdminModeSetting, facts: Facts) -> None:
    """The message names the requested mode (superuser) or each missing attribute (managed)."""
    if requested == "superuser":
        assert "superuser" in message
        return
    assert "managed" in message
    _, _, lacking = message.partition("lacks ")
    assert lacking, message
    for attribute, held in (
        ("CREATEROLE", facts.can_create_role),
        ("CREATEDB", facts.can_create_db),
    ):
        assert (attribute in lacking) is (not held), (attribute, message)


# --- a fake cluster that records every statement it is asked to execute ------------------


class Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class Cluster:
    """Fresh cluster: no managed roles, no databases. ``log`` holds every executed write."""

    def __init__(self, facts: Facts) -> None:
        self.admin_row = (
            facts.user,
            facts.is_superuser,
            facts.can_create_role,
            facts.can_create_db,
            facts.bypasses_rls,
        )
        self.roles: set[str] = set()
        self.databases: dict[str, str] = {}
        self.log: list[str] = []

    def session(self, dbname: str = "postgres") -> Session:
        return Session(self, dbname)


class Session:
    def __init__(self, cluster: Cluster, dbname: str) -> None:
        self.cluster = cluster
        self.dbname = dbname
        self.autocommit = True
        self.pgconn = None

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: object, params: tuple[Any, ...] | None = None) -> Result:
        c = self.cluster
        text = query.as_string() if isinstance(query, sql.Composable) else str(query)
        if "session_user" in text:
            return Result([c.admin_row])
        if "rolreplication" in text or "pg_auth_members" in text:
            return Result([])
        if "FROM pg_roles WHERE rolname" in text or text.startswith("SELECT 1 FROM pg_roles"):
            return Result([])
        if "FROM pg_database" in text:
            assert params is not None
            owner = c.databases.get(params[0])
            return Result([] if owner is None else [(owner,)])
        c.log.append(f"[{self.dbname}] {text}")
        if m := re.match(r'CREATE DATABASE "(\w+)" OWNER "(\w+)"', text):
            c.databases[m.group(1)] = m.group(2)
        return Result([])


def run_bootstrap(cluster: Cluster, requested: AdminModeSetting) -> str:
    """Run ``bootstrap()`` on the fake cluster; return the mode it reports."""
    config = AdminSettings.model_validate(
        {
            "CAIRN_DB_ADMIN_USER": cluster.admin_row[0],
            "CAIRN_DB_ADMIN_PASSWORD": "admin-pw-FAKE-Vb38nQ5zXe41",
            "CAIRN_DB_ADMIN_MODE": requested,
            "CAIRN_OWNER_DB_PASSWORD": "owner-pw-FAKE-Zq81xV0aLm34",
            "CAIRN_APP_DB_PASSWORD": "app-pw-FAKE-Rt55yU9cKd72",
            "CAIRN_DBOS_DB_PASSWORD": "dbos-pw-FAKE-Hn26wE3sBf90",
        }
    )
    result = bootstrap(
        cast("psycopg.Connection[Any]", cluster.session()),
        config,
        connect=lambda name: cast("psycopg.Connection[Any]", cluster.session(name)),
        encrypt=encrypt,
    )
    return result.mode


# --- properties --------------------------------------------------------------------------


@PBT
@given(requested=requested_modes, facts=admin_facts())
def test_resolve_mode_returns_the_documented_mode_or_names_what_is_missing(
    requested: AdminModeSetting, facts: Facts
) -> None:
    """Feature: cairn-free-tier-retarget, Property 5: Admin mode resolution is decided before
    any change. **Validates: Requirements 5.1, 5.2, 5.3**"""
    expected = expected_mode(requested, facts)
    if expected is None:
        with pytest.raises(BootstrapError) as err:
            resolve_mode(requested, facts)
        check_refusal(str(err.value), requested, facts)
    else:
        assert resolve_mode(requested, facts) == expected


@PBT
@given(requested=requested_modes, facts=admin_facts())
def test_bootstrap_refuses_before_executing_any_statement(
    requested: AdminModeSetting, facts: Facts
) -> None:
    """Feature: cairn-free-tier-retarget, Property 5: Admin mode resolution is decided before
    any change. **Validates: Requirements 5.1, 5.2, 5.3**"""
    cluster = Cluster(facts)
    expected = expected_mode(requested, facts)
    if expected is None:
        with pytest.raises(BootstrapError) as err:
            run_bootstrap(cluster, requested)
        check_refusal(str(err.value), requested, facts)
        assert cluster.log == []
        assert cluster.roles == set()
        assert cluster.databases == {}
    else:
        assert run_bootstrap(cluster, requested) == expected
        # The first change is the first role statement, after the mode was decided.
        assert cluster.log
        assert cluster.log[0].startswith("[postgres] CREATE ROLE")


# --- the three refusals named in the task, as fixed examples ------------------------------


@pytest.mark.parametrize(
    ("requested", "facts", "named"),
    [
        ("superuser", Facts("neondb_owner", False, True, True, True), "superuser"),
        ("managed", Facts("admin_x", False, False, True, False), "CREATEROLE"),
        ("auto", Facts("admin_x", False, True, False, False), "CREATEDB"),
    ],
)
def test_refusals_leave_the_statement_log_empty(
    requested: AdminModeSetting, facts: Facts, named: str
) -> None:
    cluster = Cluster(facts)
    with pytest.raises(BootstrapError, match=named):
        run_bootstrap(cluster, requested)
    assert cluster.log == []
