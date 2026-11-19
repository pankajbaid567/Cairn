"""Approvals, decisions proposed in Cairn, policies and attestations, the risk register (0017)."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from cairn_core.testing import FreshDb

pytestmark = pytest.mark.integration


def owner(db: FreshDb) -> psycopg.Connection[Any]:
    return db.connect_owner()


class Org:
    def __init__(self, db: FreshDb) -> None:
        self.db = db
        tag = uuid.uuid4().hex[:6]
        self.people: dict[str, tuple[str, int, str]] = {}
        with owner(db) as o:
            acct = o.execute(
                "SELECT cairn_meta.upsert_account(%s, %s, 'Own')",
                (f"s{tag}", f"own{tag}@x.example"),
            ).fetchone()
            assert acct
            org = o.execute(
                "SELECT cairn_meta.create_org(%s, %s, 'Org')", (acct[0], f"o-{tag}")
            ).fetchone()
            assert org
            self.org = int(org[0])
            pid = o.execute(
                "SELECT principal_id FROM cairn_meta.org_members WHERE org_id=%s AND account_id=%s",
                (self.org, acct[0]),
            ).fetchone()
            assert pid
            self.people["own"] = (str(acct[0]), int(pid[0]), "owner")
            for name, role in (
                ("adm", "admin"),
                ("mgr", "manager"),
                ("mgr2", "manager"),
                ("mem", "member"),
                ("aud", "auditor"),
            ):
                a = o.execute(
                    "SELECT cairn_meta.upsert_account(%s, %s, %s)",
                    (f"s{name}{tag}", f"{name}{tag}@x.example", name),
                ).fetchone()
                assert a
                p = o.execute(
                    "SELECT cairn_meta.add_member_internal(%s, %s, %s)", (self.org, a[0], role)
                ).fetchone()
                assert p
                self.people[name] = (str(a[0]), int(p[0]), role)

    def as_(self, who: str) -> psycopg.Connection[Any]:
        acct, pid, role = self.people[who]
        conn = psycopg.connect(self.db.app_dsn, autocommit=False, prepare_threshold=None)
        for name, value in (
            ("cairn.org_id", str(self.org)),
            ("cairn.account_id", acct),
            ("cairn.role", role),
            ("cairn.principal_id", str(pid)),
        ):
            conn.execute("SELECT set_config(%s, %s, false)", (name, value))
        conn.commit()
        return conn

    def propose(self, who: str, required: int = 1) -> tuple[int, str, int]:
        with self.as_(who) as c:
            row = c.execute(
                "SELECT * FROM cairn.propose_decision('Use Postgres', 'Which DB?', 'We use PostgreSQL', %s)",  # noqa: E501
                (required,),
            ).fetchone()
            c.commit()
            assert row
            return int(row[0]), str(row[1]), int(row[2])

    def status(self, decision: int) -> str:
        with owner(self.db) as o:
            row = o.execute(
                "SELECT status FROM cairn.decision_versions WHERE decision_id=%s AND upper_inf(valid_period) AND upper_inf(recorded_period)",  # noqa: E501
                (decision,),
            ).fetchone()
            assert row
            return str(row[0])


def test_a_decision_is_proposed_then_accepted_by_someone_else(fresh_db: FreshDb) -> None:
    org = Org(fresh_db)
    d, key, a = org.propose("mem")
    assert key.startswith("DR-") and org.status(d) == "proposed"  # noqa: PT018
    with (
        org.as_("mem") as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="segregation|role cannot"),  # noqa: RUF043
    ):
        c.execute("SELECT cairn.cast_vote(%s, 'approve', 'mine')", (a,))
    with org.as_("mgr") as c:
        assert c.execute("SELECT cairn.cast_vote(%s, 'approve', 'ok')", (a,)).fetchone() == (
            "approved",
        )
        c.commit()
    assert org.status(d) == "accepted"
    with owner(fresh_db) as o:
        assert o.execute(
            "SELECT verification FROM cairn.decisions WHERE decision_id=%s", (d,)
        ).fetchone() == ("verified",)
        events = [
            r[0]
            for r in o.execute(
                "SELECT event FROM cairn.decision_events WHERE decision_id=%s ORDER BY seq", (d,)
            )
        ]
    assert events == ["proposed", "accepted"]
    with org.as_("mgr") as c:
        c.execute("SELECT cairn.mark_decision_implemented(%s, 'shipped')", (d,))
        c.commit()
    assert org.status(d) == "implemented"
    with org.as_("mem") as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("SELECT cairn.mark_decision_implemented(%s, 'x')", (d,))


def test_a_manager_cannot_approve_their_own_proposal_and_a_rejection_ends_it(
    fresh_db: FreshDb,
) -> None:
    org = Org(fresh_db)
    d, _, a = org.propose("mgr")
    with (
        org.as_("mgr") as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="segregation"),
    ):
        c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a,))
    with org.as_("mgr2") as c:
        assert c.execute("SELECT cairn.cast_vote(%s, 'reject', 'not now')", (a,)).fetchone() == (
            "rejected",
        )
        c.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="already rejected"):
            c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a,))
    assert org.status(d) == "rejected"


def test_quorum_needs_distinct_approvers_and_enough_eligible_people(fresh_db: FreshDb) -> None:
    org = Org(fresh_db)
    d, _, a = org.propose("mem", required=2)
    with org.as_("mgr") as c:
        assert c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a,)).fetchone() == (
            "pending",
        )
        c.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a,))
    assert org.status(d) == "proposed"
    with org.as_("adm") as c:
        assert c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a,)).fetchone() == (
            "approved",
        )
        c.commit()
    assert org.status(d) == "accepted"
    with (
        org.as_("mem") as c,
        pytest.raises(psycopg.errors.RaiseException, match="not enough approvers"),
    ):
        c.execute("SELECT * FROM cairn.propose_decision('Big', 'q', 'outcome here', 5)")


def test_votes_cannot_be_edited_or_deleted_and_a_member_cannot_vote(fresh_db: FreshDb) -> None:
    org = Org(fresh_db)
    _, _, a = org.propose("mem")
    with org.as_("mgr") as c:
        c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a,))
        c.commit()
    with (
        owner(fresh_db) as o,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="cannot be changed"),
    ):
        o.execute("UPDATE cairn.approval_votes SET vote = 'reject' WHERE approval_id = %s", (a,))
    _, _, a2 = org.propose("mgr")
    with org.as_("mem") as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a2,))


def test_policy_versions_are_approved_published_and_never_edited(fresh_db: FreshDb) -> None:
    org = Org(fresh_db)
    with org.as_("adm") as c:
        p = c.execute(
            "SELECT cairn.create_policy('access-control', 'Access Control', 'All access is least privilege.', 365, 14)"  # noqa: E501
        ).fetchone()
        assert p
        c.commit()
        assert c.execute(
            "SELECT cairn.save_policy_draft(%s, 'Access Control', 'All access is least privilege and reviewed.', 'wording')",  # noqa: E501
            (p[0],),
        ).fetchone() == (1,)
        a = c.execute("SELECT cairn.submit_policy(%s)", (p[0],)).fetchone()
        assert a
        c.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="segregation"):
            c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a[0],))
    with org.as_("mem") as c:
        assert c.execute("SELECT count(*) FROM cairn.policies").fetchone() == (
            0,
        )  # a draft is not for members
    with org.as_("own") as c:
        assert c.execute("SELECT cairn.cast_vote(%s, 'approve', 'ok')", (a[0],)).fetchone() == (
            "approved",
        )
        c.commit()
    with org.as_("mem") as c:
        assert c.execute("SELECT state, current_version FROM cairn.policies").fetchone() == (
            "active",
            1,
        )
        assert c.execute("SELECT cairn.attest_policy(%s)", (p[0],)).fetchone() == (True,)
        assert c.execute("SELECT cairn.attest_policy(%s)", (p[0],)).fetchone() == (False,)  # once
        c.commit()
    with owner(fresh_db) as o:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="cannot be edited"):
            o.execute(
                "UPDATE cairn.policy_versions SET body = 'changed text here' WHERE policy_id = %s",
                (p[0],),
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="cannot be changed"):
            o.execute("DELETE FROM cairn.attestations")
    with org.as_(
        "adm"
    ) as c:  # a change is a new version, approved again; the old one is superseded
        assert c.execute(
            "SELECT cairn.save_policy_draft(%s, 'Access Control', 'Reviewed every quarter.', 'cadence')",  # noqa: E501
            (p[0],),
        ).fetchone() == (2,)
        a2 = c.execute("SELECT cairn.submit_policy(%s)", (p[0],)).fetchone()
        assert a2
        c.commit()
    with org.as_("own") as c:
        c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a2[0],))
        c.commit()
        states = c.execute(
            "SELECT version, state FROM cairn.policy_versions ORDER BY version"
        ).fetchall()
    assert states == [(1, "superseded"), (2, "published")]


def test_an_access_request_grants_the_role_only_after_an_admin_approves(fresh_db: FreshDb) -> None:
    org = Org(fresh_db)
    with org.as_("mem") as c:
        a = c.execute(
            "SELECT cairn.request_approval('access', 'Need manager access', '{\"role\": \"manager\"}', 1, NULL, NULL, NULL)"  # noqa: E501
        ).fetchone()
        assert a
        c.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="manager, member or auditor"):
            c.execute(
                "SELECT cairn.request_approval('access', 'Be owner', '{\"role\": \"owner\"}', 1, NULL, NULL, NULL)"  # noqa: E501
            )
    with (
        org.as_("mgr") as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):  # access needs an admin or owner
        c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a[0],))
    with org.as_("adm") as c:
        assert c.execute("SELECT cairn.cast_vote(%s, 'approve', 'ok')", (a[0],)).fetchone() == (
            "approved",
        )
        c.commit()
    with owner(fresh_db) as o:
        assert o.execute(
            "SELECT role FROM cairn_meta.org_members WHERE org_id=%s AND principal_id=%s",
            (org.org, org.people["mem"][1]),
        ).fetchone() == ("manager",)


def test_the_risk_register_is_for_leads_and_auditors_to_read(fresh_db: FreshDb) -> None:
    org = Org(fresh_db)
    pid = org.people["mgr"][1]
    with org.as_("mgr") as c:
        c.execute(
            "INSERT INTO cairn.risks (title, likelihood, impact, owner_principal, created_by) VALUES ('Vendor outage', 3, 4, %s, %s)",  # noqa: E501
            (pid, pid),
        )
        c.commit()
        assert c.execute("SELECT score FROM cairn.risks").fetchone() == (12,)
        assert c.execute("UPDATE cairn.risks SET state = 'mitigating'").rowcount == 1
    with org.as_("mem") as c:
        assert c.execute("SELECT count(*) FROM cairn.risks").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO cairn.risks (title, likelihood, impact, owner_principal, created_by) VALUES ('x y z', 1, 1, 1, 1)"  # noqa: E501
            )
    with org.as_("aud") as c:
        assert c.execute("SELECT count(*) FROM cairn.risks").fetchone() == (1,)
    with org.as_("mgr2") as c:  # a manager who does not own the risk cannot change it
        assert c.execute("UPDATE cairn.risks SET state = 'closed'").rowcount == 0


def test_governance_data_stays_inside_its_organisation(fresh_db: FreshDb) -> None:
    a, b = Org(fresh_db), Org(fresh_db)
    a.propose("mem")
    with b.as_("own") as c:
        assert c.execute("SELECT count(*) FROM cairn.approvals").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM cairn.decisions").fetchone() == (0,)
