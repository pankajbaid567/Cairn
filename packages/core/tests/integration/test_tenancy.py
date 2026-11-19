"""Tenancy (migration 0013): two organisations in one database never see each other.

The first test is a schema invariant that protects every later migration: a table in schema
`cairn` without an org_id column and a restrictive org policy fails the build.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from cairn_core.testing import FreshDb

pytestmark = pytest.mark.integration


def owner(db: FreshDb) -> psycopg.Connection[Any]:
    return db.connect_owner()


def app_in(db: FreshDb, org: int | None, account: str | None = None) -> psycopg.Connection[Any]:
    """A cairn_app connection bound to an organisation, as the API binds one per request."""
    conn = psycopg.connect(db.app_dsn, autocommit=False, prepare_threshold=None)
    # the test DSN defaults to organisation 1: always set it explicitly, empty for "none"
    conn.execute("SELECT set_config('cairn.org_id', %s, false)", ("" if org is None else str(org),))
    if account is not None:
        conn.execute("SELECT set_config('cairn.account_id', %s, false)", (account,))
    return conn


def make_org(db: FreshDb, who: str, slug: str) -> tuple[str, int]:
    """An account and an organisation it owns; names get a random suffix (the database is shared)."""  # noqa: E501
    tag = uuid.uuid4().hex[:6]
    with owner(db) as o:
        acct = o.execute(
            "SELECT cairn_meta.upsert_account(%s, %s, %s)",
            (f"sub-{tag}", f"{who}{tag}@x.example", who),
        ).fetchone()
        assert acct is not None
        org = o.execute(
            "SELECT cairn_meta.create_org(%s, %s, %s)", (acct[0], f"{slug}-{tag}", slug)
        ).fetchone()
        assert org is not None
        return str(acct[0]), int(org[0])


def seed_item(db: FreshDb, org: int, external_id: str) -> None:
    with owner(db) as o:
        o.execute(
            "INSERT INTO cairn.items (connector, external_id, source_kind, title, org_id, acl) "
            "VALUES ('docs_folder', %s, 'doc', 'secret plan', %s, '{1}')",
            (external_id, org),
        )


def test_every_tenant_table_has_org_id_and_a_restrictive_org_policy(fresh_db: FreshDb) -> None:
    with fresh_db.connect_superuser() as conn:
        tables = [
            r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'cairn'")
        ]
        assert len(tables) >= 25
        no_col = [
            t
            for t in tables
            if conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='cairn' "
                "AND table_name=%s AND column_name='org_id' AND is_nullable='NO'",
                (t,),
            ).fetchone()
            is None
        ]
        no_policy = [
            t
            for t in tables
            if conn.execute(
                "SELECT 1 FROM pg_policies WHERE schemaname='cairn' AND tablename=%s "
                "AND policyname='org_isolation' AND permissive='RESTRICTIVE'",
                (t,),
            ).fetchone()
            is None
        ]
        assert no_col == [], f"tables without org_id: {no_col}"
        assert no_policy == [], f"tables without the restrictive org policy: {no_policy}"


def test_an_organisation_cannot_read_anothers_rows(fresh_db: FreshDb) -> None:
    _, a = make_org(fresh_db, "ana", "alpha")
    _, b = make_org(fresh_db, "bo", "bravo")
    seed_item(fresh_db, a, f"doc:a{a}/plan.md")
    seed_item(fresh_db, b, f"doc:b{b}/plan.md")
    with owner(fresh_db) as o:  # the owner sees both: the control for the checks below
        both = o.execute(
            "SELECT org_id FROM cairn.items WHERE external_id LIKE 'doc:%%/plan.md'"
        ).fetchall()
        assert {r[0] for r in both} >= {a, b}
    with psycopg.connect(
        fresh_db.ingest_dsn or "", autocommit=True
    ) as ing:  # internal role: every org
        assert ing.execute("SELECT count(DISTINCT org_id) FROM cairn.items").fetchone()[0] >= 2  # type: ignore[index]
    with app_in(fresh_db, a) as ca:
        # cairn_app additionally needs ACL visibility; the org policy is checked on principals,
        # which cairn_app may read in full
        seen = {r[0] for r in ca.execute("SELECT DISTINCT org_id FROM cairn.principals")}
        assert seen == {a}
    with app_in(fresh_db, b) as cb:
        assert {r[0] for r in cb.execute("SELECT DISTINCT org_id FROM cairn.principals")} == {b}


def test_principals_of_two_organisations_can_share_a_name(fresh_db: FreshDb) -> None:
    _, a = make_org(fresh_db, "sam", "alpha")
    _, b = make_org(fresh_db, "sam", "bravo")
    with owner(fresh_db) as o:
        names = o.execute(
            "SELECT org_id, external_id FROM cairn.principals WHERE org_id = ANY(%s) AND external_id LIKE 'user:%%'",  # noqa: E501
            ([a, b],),
        ).fetchall()
    handles = {org: ext for org, ext in names}
    assert handles[a].startswith("user:sam") and handles[b].startswith("user:sam")  # noqa: PT018


def test_no_organisation_set_means_no_rows(fresh_db: FreshDb) -> None:
    make_org(fresh_db, "ana", "alpha")
    with app_in(fresh_db, None) as c:
        assert c.execute("SELECT count(*) FROM cairn.principals").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM cairn_meta.org_members").fetchone() == (0,)


def test_the_api_role_cannot_write_into_another_organisation(fresh_db: FreshDb) -> None:
    _, a = make_org(fresh_db, "ana", "alpha")
    _, b = make_org(fresh_db, "bo", "bravo")
    with app_in(fresh_db, a) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(
            "INSERT INTO cairn.audit_log (audit_id, endpoint, principal_id, principal_groups, acl_version, "  # noqa: E501
            "question_hmac, returned_item_ids, outcome, org_id) VALUES (gen_random_uuid(), 'search', 1, '{}', 1, "  # noqa: E501
            "repeat('a', 64), '{}', 'no_results', %s)",
            (b,),
        )


def test_membership_rows_are_visible_only_inside_their_organisation(fresh_db: FreshDb) -> None:
    acct_a, a = make_org(fresh_db, "ana", "alpha")
    acct_b, b = make_org(fresh_db, "bo", "bravo")
    with app_in(fresh_db, a, acct_a) as c:
        rows = c.execute("SELECT account_id::text, org_id FROM cairn_meta.org_members").fetchall()
        assert rows == [(acct_a, a)]
        orgs = c.execute("SELECT slug FROM cairn_meta.orgs").fetchall()
        assert len(orgs) == 1 and orgs[0][0].startswith("alpha-")  # noqa: PT018
    with app_in(fresh_db, b, acct_b) as c:
        assert [r[0].split("-")[0] for r in c.execute("SELECT slug FROM cairn_meta.orgs")] == [
            "bravo"
        ]


def test_the_api_role_cannot_create_an_org_for_someone_else_or_add_to_another_org(
    fresh_db: FreshDb,
) -> None:
    acct_a, a = make_org(fresh_db, "ana", "alpha")
    acct_b, b = make_org(fresh_db, "bo", "bravo")
    with app_in(fresh_db, a, acct_a) as c:  # noqa: SIM117
        with pytest.raises(psycopg.errors.RaiseException, match="signed-in account"):
            c.execute("SELECT cairn_meta.create_org(%s, 'stolen', 'Stolen')", (acct_b,))
    with app_in(fresh_db, a, acct_a) as c:  # noqa: SIM117
        with pytest.raises(psycopg.errors.RaiseException, match="current organisation"):
            c.execute("SELECT cairn_meta.add_member(%s, %s, 'member')", (b, acct_a))


def test_an_invite_is_redeemed_once_by_the_invited_email_only(fresh_db: FreshDb) -> None:
    acct_a, a = make_org(fresh_db, "ana", "alpha")
    with owner(fresh_db) as o:
        carol = o.execute(
            "SELECT cairn_meta.upsert_account('sub-c', 'carol@c.example', 'Carol')"
        ).fetchone()
        dave = o.execute(
            "SELECT cairn_meta.upsert_account('sub-d', 'dave@d.example', 'Dave')"
        ).fetchone()
        assert carol and dave  # noqa: PT018
        o.execute(
            "INSERT INTO cairn_meta.invites (org_id, email, role, token_hash, invited_by, expires_at) "  # noqa: E501
            "VALUES (%s, 'carol@c.example', 'member', 'h1', %s, now() + interval '1 day')",
            (a, acct_a),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="not valid"):
            o.execute("SELECT cairn_meta.accept_invite('h1', %s)", (dave[0],))
        assert o.execute("SELECT cairn_meta.accept_invite('h1', %s)", (carol[0],)).fetchone() == (
            a,
        )
        with pytest.raises(psycopg.errors.RaiseException, match="not valid"):
            o.execute("SELECT cairn_meta.accept_invite('h1', %s)", (carol[0],))
        role = o.execute(
            "SELECT role FROM cairn_meta.org_members WHERE org_id = %s AND account_id = %s",
            (a, carol[0]),
        ).fetchone()
        assert role == ("member",)


def test_decision_keys_are_unique_per_organisation_not_globally(fresh_db: FreshDb) -> None:
    _, a = make_org(fresh_db, "ana", "alpha")
    _, b = make_org(fresh_db, "bo", "bravo")
    with owner(fresh_db) as o:
        cols = "(key, title, origin, org_id)"
        for org in (a, b):
            o.execute(
                f"INSERT INTO cairn.decisions {cols} VALUES ('ADR-001', 't', 'manual', %s)",
                (org,),  # noqa: S608
            )
        with pytest.raises(psycopg.errors.UniqueViolation):
            o.execute(
                f"INSERT INTO cairn.decisions {cols} VALUES ('ADR-001', 't', 'manual', %s)",
                (a,),  # noqa: S608
            )


def test_older_definer_functions_stay_inside_the_callers_organisation(fresh_db: FreshDb) -> None:
    """list_reviews and my_audit run as the owner; an API session must still see only its own org."""  # noqa: E501
    acct_a, a = make_org(fresh_db, "ana", "alpha")  # noqa: RUF059
    _, b = make_org(fresh_db, "bo", "bravo")
    with owner(fresh_db) as o:
        for org in (a, b):
            o.execute(
                "INSERT INTO cairn.audit_log (audit_id, endpoint, principal_id, principal_groups, acl_version, "  # noqa: E501
                "question_hmac, returned_item_ids, outcome, org_id) "
                "SELECT gen_random_uuid(), 'search', principal_id, '{}', 1, repeat('a', 64), '{}', 'no_results', %s "  # noqa: E501
                "FROM cairn.principals "
                "WHERE org_id = %s AND kind = 'user'",
                (org, org),
            )
        pa = o.execute(
            "SELECT principal_id FROM cairn.principals WHERE org_id=%s AND kind='user'", (a,)
        ).fetchone()
        pb = o.execute(
            "SELECT principal_id FROM cairn.principals WHERE org_id=%s AND kind='user'", (b,)
        ).fetchone()
    assert pa and pb  # noqa: PT018
    with app_in(fresh_db, a) as c:
        c.execute("SELECT set_config('cairn.principal_id', %s, false)", (str(pa[0]),))
        assert len(c.execute("SELECT * FROM cairn.my_audit()").fetchall()) == 1
        # even pointing the session at the other organisation's principal id yields nothing
        c.execute("SELECT set_config('cairn.principal_id', %s, false)", (str(pb[0]),))
        assert c.execute("SELECT * FROM cairn.my_audit()").fetchall() == []
