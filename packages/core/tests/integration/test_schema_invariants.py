"""Schema invariant (Requirement 17.5): every table in schema `cairn` forces RLS.

Passes vacuously while Phase 0 has no content tables, and protects every later migration.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from cairn_core.testing import DbTestConfig, FreshDb, tables_lacking_forced_rls, temporary_database

pytestmark = pytest.mark.integration


def test_every_table_in_schema_cairn_enables_and_forces_rls(fresh_db: FreshDb) -> None:
    with fresh_db.connect_superuser() as conn:
        assert tables_lacking_forced_rls(conn) == []


def test_the_invariant_catches_tables_without_forced_rls(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """The check has teeth: enabled-but-not-forced and plain tables are both reported."""
    with temporary_database(admin_conn, admin_config) as db, db.connect_owner() as conn:
        conn.execute("CREATE TABLE cairn.plain (id integer)")
        conn.execute("CREATE TABLE cairn.enabled_only (id integer)")
        conn.execute("ALTER TABLE cairn.enabled_only ENABLE ROW LEVEL SECURITY")
        conn.execute("CREATE TABLE cairn.compliant (id integer)")
        conn.execute("ALTER TABLE cairn.compliant ENABLE ROW LEVEL SECURITY")
        conn.execute("ALTER TABLE cairn.compliant FORCE ROW LEVEL SECURITY")
        assert tables_lacking_forced_rls(conn) == ["enabled_only", "plain"]
