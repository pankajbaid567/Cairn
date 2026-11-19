"""Property tests for forbidden attributes on managed roles.

Feature: cairn-free-tier-retarget, Property 7: Forbidden attributes on managed roles are always
reported

Validates: Requirements 5.6

Two properties, both without a database:

1. ``role_attribute_violations`` returns exactly one message ``"<role> has <ATTRIBUTE>"`` for
   every (managed role, held attribute among SUPERUSER, REPLICATION, BYPASSRLS) pair and
   nothing else. Rows for unrelated roles are ignored whatever flags they carry, and the
   result does not depend on the order of the rows.
2. ``bootstrap()`` in managed mode raises ``BootstrapError`` naming every violation before the
   first change (the fake cluster's write log is empty) whenever a managed role holds any
   of the three attributes; without a violation it goes on and writes.

The fake cluster is a small copy of the one in ``test_bootstrap.py`` (that file is not
imported or edited). It answers the catalog queries ``bootstrap`` makes and records every
other statement.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, cast

import psycopg
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from psycopg import sql

from cairn_core.bootstrap import (
    DBOS_ROLE,
    MANAGED_ROLES,
    BootstrapError,
    RoleRow,
    bootstrap,
    role_attribute_violations,
)
from cairn_core.config import AdminSettings

PBT = settings(deadline=None)

# Literal copies, not imported from the module under test.
ATTRIBUTES = ("SUPERUSER", "REPLICATION", "BYPASSRLS")
ADMIN = "cairn_admin"
MANAGED_ADMIN_ROW = (ADMIN, False, True, True, True)  # not a superuser, CREATEROLE, CREATEDB
VERIFIER = "SCRAM-SHA-256$4096:c2FsdA==$c3RvcmVkLWtleQ==:c2VydmVyLWtleQ=="

Flags = tuple[bool, bool, bool]
flags = st.tuples(st.booleans(), st.booleans(), st.booleans())
unrelated_name = st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True).filter(
    lambda n: n not in MANAGED_ROLES
)


def held_attributes(attrs: Flags) -> list[str]:
    return [name for name, held in zip(ATTRIBUTES, attrs, strict=True) if held]


def expected_messages(managed: dict[str, Flags]) -> list[str]:
    """One message per held attribute of a managed role, in role-name then attribute order."""
    return [
        f"{role} has {attribute}"
        for role in sorted(managed)
        for attribute in held_attributes(managed[role])
    ]


# A subset of the three managed roles with random flags (a role may be absent from pg_roles).
managed_flags = st.dictionaries(st.sampled_from(MANAGED_ROLES), flags)
unrelated_flags = st.dictionaries(unrelated_name, flags, max_size=5)


# --- role_attribute_violations -----------------------------------------------------------


@st.composite
def role_rows(draw: st.DrawFn) -> tuple[list[RoleRow], dict[str, Flags]]:
    """Shuffled rows for some managed roles and some unrelated roles, plus the managed flags."""
    managed = draw(managed_flags)
    other = draw(unrelated_flags)
    rows = [RoleRow(name, *attrs) for name, attrs in {**managed, **other}.items()]
    return draw(st.permutations(rows)), managed


@PBT
@given(role_rows())
def test_violations_are_one_message_per_managed_role_and_held_attribute(
    drawn: tuple[list[RoleRow], dict[str, Flags]],
) -> None:
    rows, managed = drawn
    found = role_attribute_violations(rows)
    expected = expected_messages(managed)
    assert Counter(found) == Counter(expected)  # exactly these messages, none twice
    assert len(found) == len(expected)
    assert found == expected  # input order does not matter
    assert (found == []) == (not any(any(attrs) for attrs in managed.values()))


@PBT
@given(managed_flags, unrelated_flags)
def test_unrelated_roles_never_change_the_result(
    managed: dict[str, Flags], other: dict[str, Flags]
) -> None:
    only_managed = [RoleRow(name, *attrs) for name, attrs in managed.items()]
    with_others = [*only_managed, *(RoleRow(name, *attrs) for name, attrs in other.items())]
    assert role_attribute_violations(with_others) == role_attribute_violations(only_managed)


# --- bootstrap() in managed mode ---------------------------------------------------------


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Cluster:
    """Catalog state of a managed server; ``log`` holds every write in order."""

    def __init__(self, role_attrs: dict[str, Flags]) -> None:
        self.role_attrs = role_attrs  # existing managed roles -> (super, replication, bypassrls)
        self.databases: dict[str, str] = {}
        self.log: list[str] = []

    def session(self, dbname: str = "postgres") -> _Session:
        return _Session(self, dbname)


class _Session:
    def __init__(self, cluster: _Cluster, dbname: str) -> None:
        self.cluster = cluster
        self.dbname = dbname
        self.autocommit = True
        self.pgconn = None

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: object, params: tuple[Any, ...] | None = None) -> _Result:
        c = self.cluster
        text = query.as_string() if isinstance(query, sql.Composable) else str(query)
        if "session_user" in text:
            return _Result([MANAGED_ADMIN_ROW])
        if "rolreplication" in text:
            assert params is not None
            return _Result([(r, *a) for r, a in sorted(c.role_attrs.items()) if r in params[0]])
        if "FROM pg_roles WHERE rolname" in text:
            assert params is not None
            return _Result([(r,) for r in sorted(c.role_attrs) if r in params[0]])
        if "pg_auth_members" in text:
            return _Result([])
        if "FROM pg_database" in text:
            assert params is not None
            owner = c.databases.get(params[0])
            return _Result([] if owner is None else [(owner,)])
        c.log.append(f"[{self.dbname}] {text}")
        if m := re.match(r'CREATE DATABASE "(\w+)" OWNER "(\w+)"', text):
            c.databases[m.group(1)] = m.group(2)
        return _Result([])


def _settings() -> AdminSettings:
    return AdminSettings.model_validate(
        {
            "CAIRN_DB_ADMIN_USER": ADMIN,
            "CAIRN_DB_ADMIN_PASSWORD": "admin-pw-FAKE-Vb38nQ5zXe41",
            "CAIRN_DB_ADMIN_MODE": "managed",
            "CAIRN_OWNER_DB_PASSWORD": "owner-pw-FAKE-Zq81xV0aLm34",
            "CAIRN_APP_DB_PASSWORD": "app-pw-FAKE-Rt55yU9cKd72",
            "CAIRN_DBOS_DB_PASSWORD": "dbos-pw-FAKE-Hn26wE3sBf90",
        }
    )


def _run(cluster: _Cluster) -> None:
    bootstrap(
        cast("psycopg.Connection[Any]", cluster.session()),
        _settings(),
        connect=lambda name: cast("psycopg.Connection[Any]", cluster.session(name)),
        encrypt=lambda _password, _role: VERIFIER,
    )


with_violation = managed_flags.filter(lambda m: bool(expected_messages(m)))
without_violation = managed_flags.map(lambda m: {role: (False, False, False) for role in m})


@PBT
@given(with_violation)
def test_managed_bootstrap_with_any_violation_raises_before_the_first_change(
    managed: dict[str, Flags],
) -> None:
    cluster = _Cluster(managed)
    with pytest.raises(BootstrapError) as error:
        _run(cluster)
    for violation in expected_messages(managed):
        assert violation in str(error.value)
    assert cluster.log == []
    assert cluster.databases == {}


@PBT
@given(without_violation)
def test_managed_bootstrap_without_a_violation_goes_on_to_write(
    managed: dict[str, Flags],
) -> None:
    cluster = _Cluster(managed)
    _run(cluster)
    assert any("ROLE" in line for line in cluster.log)
    assert sorted(cluster.databases) == ["cairn", DBOS_ROLE]
