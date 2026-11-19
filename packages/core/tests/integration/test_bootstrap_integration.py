"""Bootstrap against the compose db (Requirements 5.1 to 5.6; needs `make up-db`).

These tests only read the shared cluster, except the second ``bootstrap`` run, which
re-asserts what is already there, and the membership test, whose seeded role and grant
live in a transaction that is rolled back (nothing else ever sees them).
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql

from cairn_core.bootstrap import (
    APP_ROLE,
    DBOS_DATABASE,
    DBOS_ROLE,
    OWNER_ROLE,
    REQUIRED_EXTENSIONS,
    BootstrapError,
    bootstrap,
    connect_like,
)
from cairn_core.testing import DbTestConfig, connect_admin

pytestmark = pytest.mark.integration

ROLES = (OWNER_ROLE, APP_ROLE, DBOS_ROLE)


def _catalog(conn: psycopg.Connection[Any], config: DbTestConfig) -> dict[str, object]:
    """Everything Requirement 5.5 lists, minus the password verifier (re-salted per run)."""
    content_db = config.settings.db_name
    roles = conn.execute(
        "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, "
        "rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil, rolconfig "
        "FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        (list(ROLES),),
    ).fetchall()
    memberships = conn.execute(
        "SELECT m.rolname, g.rolname FROM pg_auth_members am "
        "JOIN pg_roles m ON m.oid = am.member JOIN pg_roles g ON g.oid = am.roleid "
        "WHERE m.rolname = ANY(%s) OR g.rolname = ANY(%s) ORDER BY 1, 2",
        (list(ROLES), list(ROLES)),
    ).fetchall()
    databases = conn.execute(
        "SELECT d.datname, r.rolname, d.datacl::text FROM pg_database d "
        "JOIN pg_roles r ON r.oid = d.datdba WHERE d.datname = ANY(%s) ORDER BY 1",
        ([content_db, DBOS_DATABASE],),
    ).fetchall()
    with connect_like(conn, content_db) as inner:
        extensions = inner.execute("SELECT extname FROM pg_extension ORDER BY extname").fetchall()
        schemas = inner.execute(
            "SELECT nspname, nspacl::text FROM pg_namespace "
            "WHERE nspname IN ('public', 'cairn', 'cairn_meta') ORDER BY 1"
        ).fetchall()
    return {
        "roles": roles,
        "memberships": memberships,
        "databases": databases,
        "extensions": extensions,
        "schemas": schemas,
    }


def test_runtime_roles_have_no_dangerous_attributes(admin_conn: psycopg.Connection[Any]) -> None:
    """5.1: three LOGIN roles without superuser, createdb, createrole, replication or bypassrls."""
    rows = admin_conn.execute(
        "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls FROM pg_roles WHERE rolname = ANY(%s)",
        (list(ROLES),),
    ).fetchall()
    assert {r[0] for r in rows} == set(ROLES)
    for name, can_login, *dangerous in rows:
        assert can_login, name
        assert not any(dangerous), name


def test_databases_are_owned_and_connectable_by_the_intended_roles(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """5.2 and 5.4: owners, and CONNECT for the intended roles only (none for PUBLIC)."""
    content_db = admin_config.settings.db_name
    owners = dict(
        admin_conn.execute(
            "SELECT d.datname, r.rolname FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba "
            "WHERE d.datname = ANY(%s)",
            ([content_db, DBOS_DATABASE],),
        ).fetchall()
    )
    assert owners == {content_db: OWNER_ROLE, DBOS_DATABASE: DBOS_ROLE}

    def can(role: str, database: str, privilege: str) -> bool:
        row = admin_conn.execute(
            "SELECT has_database_privilege(%s, %s, %s)", (role, database, privilege)
        ).fetchone()
        assert row is not None
        return bool(row[0])

    assert can(OWNER_ROLE, content_db, "CONNECT")
    assert can(APP_ROLE, content_db, "CONNECT")
    assert not can(DBOS_ROLE, content_db, "CONNECT")
    assert can(DBOS_ROLE, DBOS_DATABASE, "CONNECT")
    assert not can(OWNER_ROLE, DBOS_DATABASE, "CONNECT")
    assert not can(APP_ROLE, DBOS_DATABASE, "CONNECT")
    for database in (content_db, DBOS_DATABASE):
        acl = admin_conn.execute(
            "SELECT coalesce(datacl, '{}')::text[] FROM pg_database WHERE datname = %s",
            (database,),
        ).fetchone()
        assert acl is not None
        assert not [entry for entry in acl[0] if entry.startswith("=")], f"PUBLIC on {database}"
    # TEMPORARY went away with PUBLIC; only an owner keeps it.
    assert not can(APP_ROLE, content_db, "TEMPORARY")
    assert not can(APP_ROLE, DBOS_DATABASE, "TEMPORARY")
    assert not can(DBOS_ROLE, content_db, "TEMPORARY")


def test_content_database_has_the_required_extensions(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """5.3 and retarget Requirement 2.1: exactly vector and btree_gist (plus built-in plpgsql)."""
    with connect_like(admin_conn, admin_config.settings.db_name) as inner:
        installed = {r[0] for r in inner.execute("SELECT extname FROM pg_extension").fetchall()}
    assert installed - {"plpgsql"} == set(REQUIRED_EXTENSIONS)


@pytest.mark.managed  # also runs against ft-db with CAIRN_TEST_ADMIN_MODE=managed (task 6.2)
def test_bootstrap_twice_leaves_the_catalog_unchanged(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """5.5: attributes, memberships, database owners and privileges, extensions."""
    first = bootstrap(admin_conn, admin_config.settings)
    before = _catalog(admin_conn, admin_config)
    second = bootstrap(admin_conn, admin_config.settings)
    after = _catalog(admin_conn, admin_config)
    assert after == before
    assert [r.name for r in second.roles] == [r.name for r in first.roles]
    assert not any(r.created for r in second.roles)
    assert not any(d.created for d in second.databases)


SEEDED_GROUP = "cairn_test_grp_probe"


def _bootstrap_with_seeded_membership(config: DbTestConfig, member: str) -> None:
    """Seed a role and a membership, run bootstrap, and always roll the seed back."""
    conn = connect_admin(config)
    try:
        with conn.transaction():
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(SEEDED_GROUP)))
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(SEEDED_GROUP), sql.Identifier(member)
                )
            )
            bootstrap(conn, config.settings)
            raise psycopg.Rollback  # bootstrap should have refused; never commit the seed
    finally:
        conn.close()


@pytest.mark.parametrize("member", [APP_ROLE, DBOS_ROLE])
def test_membership_of_a_runtime_role_fails_bootstrap_and_names_it(
    member: str,
    admin_config: DbTestConfig,
    admin_conn: psycopg.Connection[Any],  # skips the test when the db is down
) -> None:
    """5.6: the membership is seeded in a transaction on its own connection that always rolls
    back, so no other session (and no concurrent bootstrap) can ever see it. Bootstrap checks
    memberships before it writes anything."""
    with pytest.raises(BootstrapError) as raised:
        _bootstrap_with_seeded_membership(admin_config, member)
    message = str(raised.value)
    assert member in message
    assert SEEDED_GROUP in message
    for secret in (
        admin_config.settings.owner_db_password.get_secret_value(),
        admin_config.settings.app_db_password.get_secret_value(),
        admin_config.settings.dbos_db_password.get_secret_value(),
    ):
        assert secret not in message


def test_the_seeded_membership_did_not_survive(admin_conn: psycopg.Connection[Any]) -> None:
    """The rollback above left no role or membership behind in the shared cluster."""
    rows = admin_conn.execute(
        "SELECT count(*) FROM pg_roles WHERE rolname = 'cairn_test_grp_probe'"
    ).fetchone()
    assert rows == (0,)
