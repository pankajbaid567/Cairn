"""Role and default-deny test (design §15, Requirements 17.2 to 17.4; plan item 0.17).

The six steps of the design, run in a database of their own so the probe table never
appears in the shared ``fresh_db``:

1. ``cairn_app`` has none of the dangerous role attributes.
2. ``cairn_app`` owns no object in the database.
3. ``cairn_app`` is a member of no role, directly or through other roles.
4. A forced-RLS table without a policy is created by the owner.
5. App and owner see zero rows. The admin sees all rows: in superuser mode (the compose
   ``db``) the superuser does, in managed mode (``ft-db``, ``CAIRN_TEST_ADMIN_MODE=managed``,
   free-tier Requirements 6.4 to 6.6) the NOSUPERUSER BYPASSRLS ``cairn_admin`` does.
6. ``cairn_app`` cannot disable RLS and cannot switch ``row_security`` off.

Every test runs in both modes except the two admin tests of step 5, each of which skips in
the other mode with a message. ``make matrix`` runs the default mode; ``make matrix-managed``
(task 6.2) runs the managed one.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import errors, sql

from cairn_core.testing import DbTestConfig, FreshDb, temporary_database

pytestmark = [pytest.mark.integration, pytest.mark.matrix]

DANGEROUS_ATTRIBUTES = (
    "rolsuper",
    "rolbypassrls",
    "rolcreaterole",
    "rolcreatedb",
    "rolreplication",
)
RUNTIME_ROLES = ("cairn_app", "cairn_owner", "cairn_dbos")


@pytest.fixture(scope="module")
def role_db(admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig) -> Iterator[FreshDb]:
    with temporary_database(admin_conn, admin_config) as db:
        yield db


@pytest.fixture(scope="module")
def probe_db(role_db: FreshDb, admin_config: DbTestConfig) -> FreshDb:
    """Step 4: ``cairn.rls_probe`` with three rows, RLS enabled and forced, and no policy.

    In managed mode the admin is neither the owner nor a superuser, so it also needs USAGE on
    the schema and SELECT on the table to read anything (FV4 row 11)."""
    with role_db.connect_owner() as owner:
        owner.execute("CREATE TABLE cairn.rls_probe (id integer PRIMARY KEY, note text)")
        owner.execute("INSERT INTO cairn.rls_probe VALUES (1, 'a'), (2, 'b'), (3, 'c')")
        owner.execute("ALTER TABLE cairn.rls_probe ENABLE ROW LEVEL SECURITY")
        owner.execute("ALTER TABLE cairn.rls_probe FORCE ROW LEVEL SECURITY")
        owner.execute("GRANT SELECT ON cairn.rls_probe TO cairn_app")
        if admin_config.target.mode == "managed":
            admin = sql.Identifier(admin_config.target.user)
            owner.execute(sql.SQL("GRANT USAGE ON SCHEMA cairn TO {}").format(admin))
            owner.execute(sql.SQL("GRANT SELECT ON cairn.rls_probe TO {}").format(admin))
    return role_db


def _count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT count(*) FROM cairn.rls_probe").fetchone()
    assert row is not None
    return int(row[0])


# Step 1


@pytest.mark.parametrize("role", RUNTIME_ROLES)
def test_role_has_no_dangerous_attributes(admin_conn: psycopg.Connection[Any], role: str) -> None:
    columns = ", ".join(DANGEROUS_ATTRIBUTES)
    row = admin_conn.execute(
        f"SELECT {columns} FROM pg_roles WHERE rolname = %s",  # noqa: S608
        (role,),
    ).fetchone()
    assert row is not None, f"role {role} does not exist"
    assert dict(zip(DANGEROUS_ATTRIBUTES, row, strict=True)) == dict.fromkeys(
        DANGEROUS_ATTRIBUTES, False
    )


# Step 2


def test_app_owns_no_object_in_the_database(role_db: FreshDb) -> None:
    with role_db.connect_admin() as conn:
        owned = conn.execute(
            """
            SELECT 'pg_class', relname::text FROM pg_class WHERE relowner = 'cairn_app'::regrole
            UNION ALL
            SELECT 'pg_namespace', nspname::text FROM pg_namespace
                WHERE nspowner = 'cairn_app'::regrole
            UNION ALL
            SELECT 'pg_proc', proname::text FROM pg_proc WHERE proowner = 'cairn_app'::regrole
            UNION ALL
            SELECT 'pg_type', typname::text FROM pg_type WHERE typowner = 'cairn_app'::regrole
            """
        ).fetchall()
    assert owned == []


# Step 3


def test_app_is_a_member_of_no_role_recursively(admin_conn: psycopg.Connection[Any]) -> None:
    for role in ("cairn_app", "cairn_dbos"):
        reachable = admin_conn.execute(
            """
            WITH RECURSIVE parents(oid) AS (
                SELECT roleid FROM pg_auth_members WHERE member = %s::regrole
                UNION
                SELECT m.roleid FROM pg_auth_members m JOIN parents p ON m.member = p.oid
            )
            SELECT oid::regrole::text FROM parents
            """,
            (role,),
        ).fetchall()
        assert reachable == [], f"{role} is a member of {reachable}"


def test_app_cannot_set_role_to_the_owner(role_db: FreshDb) -> None:
    with role_db.connect_app() as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SET ROLE cairn_owner")


# Steps 4 and 5


def test_probe_table_forces_rls_and_has_no_policy(probe_db: FreshDb) -> None:
    with probe_db.connect_admin() as conn:
        flags = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'cairn.rls_probe'::regclass"
        ).fetchone()
        policies = conn.execute(
            "SELECT count(*) FROM pg_policies WHERE schemaname = 'cairn' "
            "AND tablename = 'rls_probe'"
        ).fetchone()
    assert flags == (True, True)
    assert policies == (0,)


def test_app_sees_zero_rows_without_a_policy(probe_db: FreshDb) -> None:
    with probe_db.connect_app() as conn:
        assert _count(conn) == 0


def test_owner_sees_zero_rows_without_a_policy(probe_db: FreshDb) -> None:
    with probe_db.connect_owner() as conn:
        assert _count(conn) == 0


def test_superuser_sees_all_rows(probe_db: FreshDb, admin_config: DbTestConfig) -> None:
    if admin_config.target.mode != "superuser":
        pytest.skip("superuser mode only; managed mode runs test_admin_bypasses_rls_...")
    with probe_db.connect_admin() as conn:
        assert _count(conn) == 3


@pytest.mark.managed
def test_admin_bypasses_rls_and_is_not_a_runtime_role(
    probe_db: FreshDb, admin_config: DbTestConfig
) -> None:
    """Requirement 6.6: the provider-style admin is no superuser, is none of the runtime
    roles, and reads every row of the forced-RLS table because it has BYPASSRLS."""
    if admin_config.target.mode != "managed":
        pytest.skip("needs CAIRN_TEST_ADMIN_MODE=managed and ft-db (make matrix-managed)")
    with probe_db.connect_admin() as conn:
        row = conn.execute(
            "SELECT session_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = session_user"
        ).fetchone()
        assert row is not None
        user, is_superuser, bypasses_rls = row
        assert user == admin_config.target.user
        assert user not in RUNTIME_ROLES
        assert is_superuser is False
        assert bypasses_rls is True
        assert _count(conn) == 3


# Step 6


def test_app_cannot_disable_row_level_security(probe_db: FreshDb) -> None:
    with probe_db.connect_app() as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("ALTER TABLE cairn.rls_probe DISABLE ROW LEVEL SECURITY")


def test_row_security_off_errors_instead_of_filtering(probe_db: FreshDb) -> None:
    with probe_db.connect_app() as conn:
        conn.execute("SET row_security = off")
        with pytest.raises(errors.InsufficientPrivilege):
            _count(conn)
