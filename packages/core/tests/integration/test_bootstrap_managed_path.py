"""The managed-mode statements against a real server (needs `make up-db`).

The compose `db` admin is a superuser, so this forces the managed code path on it
(`mode="managed"`). That proves every managed statement is valid SQL in the order the builders
emit it (SET ROLE / RESET ROLE pairs, the admin's CONNECT and temporary CREATE grants). What a
real non-superuser admin may do is recorded in docs/evidence/free-tier/verify/FV4.md and runs
end to end on `ft-db` in task 6.2. Only `cairn_test_*` databases are created, and they are dropped.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

from cairn_core.bootstrap import (
    APP_ROLE,
    OWNER_ROLE,
    REQUIRED_EXTENSIONS,
    bootstrap_database,
    connect_like,
    dbos_spec,
)
from cairn_core.testing import TEST_DB_PREFIX, drop_database

pytestmark = pytest.mark.integration


@contextmanager
def scratch_name(admin_conn: psycopg.Connection[Any]) -> Iterator[str]:
    name = TEST_DB_PREFIX + secrets.token_hex(4)
    try:
        yield name
    finally:
        drop_database(admin_conn, name)


def _acl(conn: psycopg.Connection[Any], name: str) -> list[str]:
    row = conn.execute(
        "SELECT datacl::text[] FROM pg_database WHERE datname = %s", (name,)
    ).fetchone()
    assert row is not None
    assert row[0] is not None
    return sorted(row[0])


def test_managed_path_builds_a_content_database_and_is_repeatable(
    admin_conn: psycopg.Connection[Any],
) -> None:
    with scratch_name(admin_conn) as name:
        first = bootstrap_database(admin_conn, name, mode="managed")
        assert first.created
        assert first.extensions == REQUIRED_EXTENSIONS
        acl = _acl(admin_conn, name)
        # The admin keeps CONNECT; the temporary CREATE grant is gone again.
        admin = admin_conn.info.user
        admin_entries = [e for e in acl if e.startswith(f"{admin}=")]
        assert admin_entries == [f"{admin}=c/{OWNER_ROLE}"]
        assert not [e for e in acl if e.startswith("=")], "PUBLIC must not keep a privilege"
        assert any(e.startswith(f"{APP_ROLE}=c/") for e in acl)
        with connect_like(admin_conn, name) as inner:
            extensions = {
                r[0] for r in inner.execute("SELECT extname FROM pg_extension").fetchall()
            }
            public_acl = inner.execute(
                "SELECT nspacl::text FROM pg_namespace WHERE nspname = 'public'"
            ).fetchone()
        assert set(REQUIRED_EXTENSIONS) <= extensions
        assert public_acl is not None
        public_entries = public_acl[0].strip("{}").split(",")
        assert [e for e in public_entries if e.startswith("=")] == ["=U/pg_database_owner"], (
            "PUBLIC keeps USAGE on public but no CREATE"
        )
        second = bootstrap_database(admin_conn, name, mode="managed")
        assert not second.created
        assert _acl(admin_conn, name) == acl
        role = admin_conn.execute("SELECT current_user, session_user").fetchone()
        assert role is not None
        assert role[0] == role[1], "SET ROLE must not leak out of bootstrap"


def test_managed_path_builds_a_database_without_extensions(
    admin_conn: psycopg.Connection[Any],
) -> None:
    with scratch_name(admin_conn) as name:
        result = bootstrap_database(admin_conn, name, spec=dbos_spec(name), mode="managed")
        assert result.created
        assert result.extensions == ()
        acl = _acl(admin_conn, name)
        admin = admin_conn.info.user
        assert [e for e in acl if e.startswith(f"{admin}=")] == [f"{admin}=c/cairn_dbos"]
        role = admin_conn.execute("SELECT current_user, session_user").fetchone()
        assert role is not None
        assert role[0] == role[1]
