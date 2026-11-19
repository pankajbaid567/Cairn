"""People administration rules and the hash-chained audit-event log (migration 0014)."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from cairn_core.testing import FreshDb

pytestmark = pytest.mark.integration


def owner(db: FreshDb) -> psycopg.Connection[Any]:
    return db.connect_owner()


def app(
    db: FreshDb, org: int, account: str, role: str, principal: int | None = None
) -> psycopg.Connection[Any]:
    """A cairn_app connection with the request settings the API writes."""
    conn = psycopg.connect(db.app_dsn, autocommit=False, prepare_threshold=None)
    for name, value in (
        ("cairn.org_id", str(org)),
        ("cairn.account_id", account),
        ("cairn.role", role),
        ("cairn.principal_id", "" if principal is None else str(principal)),
    ):
        conn.execute("SELECT set_config(%s, %s, false)", (name, value))
    conn.commit()  # a later rollback must not undo the settings
    return conn


def new_org(db: FreshDb, who: str = "own") -> tuple[str, int, int]:
    tag = uuid.uuid4().hex[:6]
    with owner(db) as o:
        acct = o.execute(
            "SELECT cairn_meta.upsert_account(%s, %s, %s)",
            (f"s{tag}", f"{who}{tag}@x.example", who),
        ).fetchone()
        assert acct
        org = o.execute(
            "SELECT cairn_meta.create_org(%s, %s, %s)", (acct[0], f"o-{tag}", "Org")
        ).fetchone()
        assert org
        pid = o.execute(
            "SELECT principal_id FROM cairn_meta.org_members WHERE org_id=%s AND account_id=%s",
            (org[0], acct[0]),
        ).fetchone()
        assert pid
        return str(acct[0]), int(org[0]), int(pid[0])


def add(db: FreshDb, org: int, who: str, role: str) -> tuple[str, int]:
    tag = uuid.uuid4().hex[:6]
    with owner(db) as o:
        acct = o.execute(
            "SELECT cairn_meta.upsert_account(%s, %s, %s)",
            (f"s{tag}", f"{who}{tag}@x.example", who),
        ).fetchone()
        assert acct
        pid = o.execute(
            "SELECT cairn_meta.add_member_internal(%s, %s, %s)", (org, acct[0], role)
        ).fetchone()
        assert pid
        return str(acct[0]), int(pid[0])


# --- audit chain ----------------------------------------------------------------------------


def test_events_form_a_chain_that_verifies(fresh_db: FreshDb) -> None:
    acct, org, pid = new_org(fresh_db)
    with app(fresh_db, org, acct, "owner", pid) as c:
        for i in range(5):
            c.execute(
                "SELECT cairn.record_event('member.invited', 'invite', %s, '{\"n\": 1}')", (str(i),)
            )
        c.commit()
        ok, checked, bad, head_seq, head = c.execute(
            "SELECT * FROM cairn.verify_chain()"
        ).fetchone()  # type: ignore[misc]
    assert (ok, checked, bad, head_seq) == (True, 5, None, 5)
    assert head and len(head) == 64  # noqa: PT018


def test_editing_a_row_is_refused_and_a_bypassed_edit_is_detected(fresh_db: FreshDb) -> None:
    acct, org, pid = new_org(fresh_db)
    with app(fresh_db, org, acct, "owner", pid) as c:
        for i in range(4):
            c.execute("SELECT cairn.record_event('policy.created', 'policy', %s)", (str(i),))
        c.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE cairn.audit_events SET action = 'policy.deleted' WHERE org_seq = 2")
        c.rollback()
    with (
        owner(fresh_db) as o,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"),
    ):
        o.execute("DELETE FROM cairn.audit_events WHERE org_id = %s AND org_seq = 2", (org,))
    # A database administrator who disables the triggers can still edit; the chain shows it.
    with fresh_db.connect_superuser() as su:
        su.execute("SET session_replication_role = replica")
        su.execute(
            "UPDATE cairn.audit_events SET target_id = 'x' WHERE org_id = %s AND org_seq = 3",
            (org,),
        )
    with app(fresh_db, org, acct, "auditor", pid) as c:
        ok, checked, bad, _, _ = c.execute("SELECT * FROM cairn.verify_chain()").fetchone()  # type: ignore[misc]  # noqa: RUF059
    assert (ok, bad) == (False, 3)


def test_a_deleted_row_is_detected_as_a_gap(fresh_db: FreshDb) -> None:
    acct, org, pid = new_org(fresh_db)
    with app(fresh_db, org, acct, "owner", pid) as c:
        for i in range(4):
            c.execute("SELECT cairn.record_event('policy.created', 'policy', %s)", (str(i),))
        c.commit()
    with fresh_db.connect_superuser() as su:
        su.execute("SET session_replication_role = replica")
        su.execute("DELETE FROM cairn.audit_events WHERE org_id = %s AND org_seq = 2", (org,))
    with app(fresh_db, org, acct, "owner", pid) as c:
        ok, _, bad, _, _ = c.execute("SELECT * FROM cairn.verify_chain()").fetchone()  # type: ignore[misc]
    assert (ok, bad) == (False, 3)


def test_chains_are_per_organisation_and_readable_by_role(fresh_db: FreshDb) -> None:
    a_acct, a, a_pid = new_org(fresh_db)
    b_acct, b, b_pid = new_org(fresh_db)
    m_acct, m_pid = add(fresh_db, a, "mem", "member")
    with app(fresh_db, a, a_acct, "owner", a_pid) as c:
        c.execute("SELECT cairn.record_event('org.updated')")
        c.commit()
    with app(fresh_db, b, b_acct, "owner", b_pid) as c:
        c.execute("SELECT cairn.record_event('org.updated')")
        c.execute("SELECT cairn.record_event('org.updated')")
        c.commit()
        assert c.execute("SELECT count(*) FROM cairn.audit_events").fetchone() == (2,)
    with app(fresh_db, a, m_acct, "member", m_pid) as c:  # a member sees only their own events
        assert c.execute("SELECT count(*) FROM cairn.audit_events").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM cairn.verify_chain()")


# --- people rules ---------------------------------------------------------------------------


def test_roles_follow_the_rules(fresh_db: FreshDb) -> None:
    o_acct, org, o_pid = new_org(fresh_db)
    ad_acct, ad_pid = add(fresh_db, org, "adm", "admin")
    me_acct, me_pid = add(fresh_db, org, "mem", "member")
    with app(fresh_db, org, o_acct, "owner", o_pid) as c:
        c.execute("SELECT cairn_meta.set_member_role(%s, 'manager')", (me_acct,))
        c.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="own role"):
            c.execute("SELECT cairn_meta.set_member_role(%s, 'member')", (o_acct,))
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="yourself"):
            c.execute("SELECT cairn_meta.set_member_state(%s, false)", (o_acct,))
    with app(fresh_db, org, ad_acct, "admin", ad_pid) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT cairn_meta.set_member_role(%s, 'member')", (o_acct,))
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT cairn_meta.set_member_role(%s, 'admin')", (me_acct,))
        c.rollback()
        c.execute("SELECT cairn_meta.set_member_role(%s, 'auditor')", (me_acct,))
    with (
        app(fresh_db, org, me_acct, "auditor", me_pid) as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        c.execute("SELECT cairn_meta.set_member_role(%s, 'owner')", (me_acct,))


def test_a_deactivated_member_loses_group_membership_now(fresh_db: FreshDb) -> None:
    o_acct, org, o_pid = new_org(fresh_db)
    m_acct, m_pid = add(fresh_db, org, "mem", "member")
    with owner(fresh_db) as o:
        before = o.execute(
            "SELECT count(*) FROM cairn.memberships WHERE member_id=%s AND valid_period @> now()",
            (m_pid,),
        ).fetchone()
    assert before == (1,)
    with app(fresh_db, org, o_acct, "owner", o_pid) as c:
        c.execute("SELECT cairn_meta.set_member_state(%s, false)", (m_acct,))
        c.commit()
    with owner(fresh_db) as o:
        after = o.execute(
            "SELECT count(*) FROM cairn.memberships WHERE member_id=%s AND valid_period @> now()",
            (m_pid,),
        ).fetchone()
        st = o.execute(
            "SELECT state FROM cairn_meta.org_members WHERE account_id=%s AND org_id=%s",
            (m_acct, org),
        ).fetchone()
    assert after == (0,) and st == ("deactivated",)  # noqa: PT018


def test_invites_need_an_admin_and_cannot_target_an_existing_member(fresh_db: FreshDb) -> None:
    o_acct, org, o_pid = new_org(fresh_db)
    m_acct, m_pid = add(fresh_db, org, "mem", "member")
    with (
        app(fresh_db, org, m_acct, "member", m_pid) as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        c.execute("SELECT cairn_meta.create_invite('new@x.example', 'member', 'hash-m', 24)")
    with app(fresh_db, org, o_acct, "owner", o_pid) as c:
        c.execute(
            "SELECT cairn_meta.create_invite('new@x.example', 'member', %s, 24)",
            (uuid.uuid4().hex,),
        )
        c.commit()
        assert c.execute(
            "SELECT count(*) FROM cairn_meta.invites WHERE state = 'pending'"
        ).fetchone() == (1,)
        # a second invite to the same address replaces the first
        c.execute(
            "SELECT cairn_meta.create_invite('new@x.example', 'manager', %s, 24)",
            (uuid.uuid4().hex,),
        )
        c.commit()
        assert c.execute(
            "SELECT count(*) FROM cairn_meta.invites WHERE state = 'pending'"
        ).fetchone() == (1,)
    with (
        app(fresh_db, org, o_acct, "admin", o_pid) as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        c.execute("SELECT cairn_meta.create_invite('adm@x.example', 'admin', 'h2', 24)")


def test_teams_and_the_directory(fresh_db: FreshDb) -> None:
    o_acct, org, o_pid = new_org(fresh_db)
    m_acct, m_pid = add(fresh_db, org, "mem", "member")
    g_acct, g_pid = add(fresh_db, org, "gst", "guest")
    with app(fresh_db, org, o_acct, "owner", o_pid) as c:
        team = c.execute("SELECT cairn_meta.create_team('payments', 'Payments')").fetchone()
        assert team
        c.execute("SELECT cairn_meta.team_set_member(%s, %s, true)", (team[0], m_pid))
        c.commit()
        rows = c.execute(
            "SELECT principal_id, slug, name, member_count FROM cairn_meta.teams()"
        ).fetchall()
        assert rows == [(team[0], "payments", "Payments", 1)]
        directory = c.execute(
            "SELECT account_id::text, team_ids FROM cairn_meta.directory()"
        ).fetchall()
        assert len(directory) == 3 and dict(directory)[m_acct] == [team[0]]  # noqa: PT018
    with app(fresh_db, org, g_acct, "guest", g_pid) as c:
        assert len(c.execute("SELECT * FROM cairn_meta.directory()").fetchall()) == 1
