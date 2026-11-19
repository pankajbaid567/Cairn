"""Access reviews and the control checks (migration 0018)."""

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
    def __init__(self, db: FreshDb, people: dict[str, str]) -> None:
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
            for name, role in people.items():
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

    def items(self, review: int) -> list[tuple[int, str, str, int]]:
        with owner(self.db) as o:
            rows = o.execute(
                "SELECT item_id, member_name, decision, reviewer_principal FROM cairn.access_review_items WHERE review_id=%s ORDER BY item_id",  # noqa: E501
                (review,),
            ).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2]), int(r[3])) for r in rows]


def test_a_review_needs_two_privileged_people_and_nobody_reviews_themselves(
    fresh_db: FreshDb,
) -> None:
    solo = Org(fresh_db, {"mem": "member"})
    with solo.as_("own") as c, pytest.raises(psycopg.errors.RaiseException, match="at least two"):
        c.execute("SELECT cairn.start_access_review('Q4 review', 14)")
    org = Org(fresh_db, {"adm": "admin", "mgr": "manager", "mem": "member", "gst": "guest"})
    with org.as_("own") as c:
        rid = c.execute("SELECT cairn.start_access_review('Q4 review', 14)").fetchone()
        assert rid
        c.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):  # one open review at a time
            c.execute("SELECT cairn.start_access_review('again', 14)")
    items = org.items(int(rid[0]))
    assert len(items) == 5
    by_name = {name: (iid, reviewer) for iid, name, _, reviewer in items}
    pids = {n: p for n, (_, p, _) in org.people.items()}
    assert all(
        reviewer != pids.get(name.lower(), -1)
        for name, (_, reviewer) in by_name.items()
        if name.lower() in pids
    )
    assert all(reviewer in (pids["own"], pids["adm"]) for _, reviewer in by_name.values())


def test_only_the_assigned_reviewer_decides_and_the_decision_takes_effect(
    fresh_db: FreshDb,
) -> None:
    org = Org(fresh_db, {"adm": "admin", "mgr": "manager", "mem": "member"})
    with org.as_("own") as c:
        rid = c.execute("SELECT cairn.start_access_review('Q4 review', 14)").fetchone()
        assert rid
        c.commit()
    pid = {n: p for n, (_, p, _) in org.people.items()}
    items = {name: (iid, reviewer) for iid, name, _, reviewer in org.items(int(rid[0]))}
    mem_item, mem_reviewer = items["mem"]
    reviewer_name = next(n for n, p in pid.items() if p == mem_reviewer)
    other = "adm" if reviewer_name == "own" else "own"
    with (
        org.as_(other) as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="assigned reviewer"),
    ):
        c.execute("SELECT cairn.decide_review_item(%s, 'remove', NULL, 'no')", (mem_item,))
    with org.as_(reviewer_name) as c:
        c.execute(
            "SELECT cairn.decide_review_item(%s, 'remove', NULL, 'left the project')", (mem_item,)
        )
        c.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="already decided"):
            c.execute("SELECT cairn.decide_review_item(%s, 'keep', NULL, NULL)", (mem_item,))
    with owner(fresh_db) as o:
        assert o.execute(
            "SELECT state FROM cairn_meta.org_members WHERE org_id=%s AND principal_id=%s",
            (org.org, pid["mem"]),
        ).fetchone() == ("deactivated",)
        assert o.execute(
            "SELECT count(*) FROM cairn.memberships WHERE member_id=%s AND valid_period @> now()",
            (pid["mem"],),
        ).fetchone() == (0,)


def test_a_review_closes_only_when_every_item_is_decided_and_an_admin_cannot_touch_an_owner(
    fresh_db: FreshDb,
) -> None:
    org = Org(fresh_db, {"adm": "admin", "mem": "member"})
    pid = {n: p for n, (_, p, _) in org.people.items()}
    with org.as_("own") as c:
        rid = int(c.execute("SELECT cairn.start_access_review('Q4 review', 14)").fetchone()[0])  # type: ignore[index]
        c.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="every item must be decided"):
            c.execute("SELECT cairn.close_access_review(%s)", (rid,))
    items = org.items(rid)
    owner_item = next(i for i in items if i[1] == "Own")
    reviewer_name = next(n for n, p in pid.items() if p == owner_item[3])
    assert reviewer_name == "adm"  # the owner cannot review themselves, so the administrator does
    with (
        org.as_("adm") as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="only an owner"),
    ):
        c.execute(
            "SELECT cairn.decide_review_item(%s, 'change', 'member', 'demote')", (owner_item[0],)
        )
    for iid, name, _, reviewer in items:  # noqa: B007
        who = next(n for n, p in pid.items() if p == reviewer)
        with org.as_(who) as c:
            c.execute("SELECT cairn.decide_review_item(%s, 'keep', NULL, 'ok')", (iid,))
            c.commit()
    with org.as_("own") as c:
        c.execute("SELECT cairn.close_access_review(%s)", (rid,))
        c.commit()
        assert c.execute("SELECT state FROM cairn.access_reviews").fetchone() == ("closed",)
    with org.as_("mem") as c:
        assert c.execute("SELECT count(*) FROM cairn.access_reviews").fetchone() == (0,)


def statuses(org: Org, who: str = "own") -> dict[str, str]:
    with org.as_(who) as c:
        return {r[0]: r[3] for r in c.execute("SELECT * FROM cairn.control_status()")}


def test_controls_start_without_evidence_and_turn_to_pass_as_the_platform_is_used(
    fresh_db: FreshDb,
) -> None:
    org = Org(fresh_db, {"adm": "admin", "mgr": "manager", "mem": "member", "aud": "auditor"})
    s = statuses(org)
    assert (  # noqa: PT018
        s["POL-1"] == "no_evidence" and s["ACC-1"] == "no_evidence" and s["CHG-1"] == "no_evidence"
    )
    assert s["ACC-2"] == "pass" and s["LOG-1"] in ("pass", "no_evidence")  # noqa: PT018
    # a policy through approval, an access review, an approval: the matching controls pass
    with org.as_("adm") as c:
        p = c.execute(
            "SELECT cairn.create_policy('access', 'Access Control', 'Least privilege applies to everyone.', 365, 30)"  # noqa: E501
        ).fetchone()
        assert p
        a = c.execute("SELECT cairn.submit_policy(%s)", (p[0],)).fetchone()
        assert a
        c.commit()
    with org.as_("own") as c:
        c.execute("SELECT cairn.cast_vote(%s, 'approve', NULL)", (a[0],))
        c.execute("SELECT cairn.record_event('policy.published', 'policy', '1')")
        c.commit()
    s = statuses(org, "aud")
    assert (  # noqa: PT018
        s["POL-1"] == "pass"
        and s["CHG-1"] == "pass"
        and s["LOG-1"] == "pass"
        and s["LOG-2"] == "pass"
    )
    assert s["POL-2"] == "pass"  # the attestation date is still ahead
    with org.as_("mem") as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("SELECT * FROM cairn.control_status()")


def test_a_broken_chain_fails_the_logging_control(fresh_db: FreshDb) -> None:
    org = Org(fresh_db, {"adm": "admin"})
    with org.as_("own") as c:
        for i in range(3):
            c.execute("SELECT cairn.record_event('org.updated', 'org', %s)", (str(i),))
        c.commit()
    assert statuses(org)["LOG-1"] == "pass"
    with fresh_db.connect_superuser() as su:
        su.execute("SET session_replication_role = replica")
        su.execute(
            "UPDATE cairn.audit_events SET target_id = 'x' WHERE org_id = %s AND org_seq = 2",
            (org.org,),
        )
    assert statuses(org)["LOG-1"] == "fail"


def test_least_privilege_fails_when_everyone_is_an_admin(fresh_db: FreshDb) -> None:
    org = Org(fresh_db, {f"a{i}": "admin" for i in range(5)} | {"mem": "member"})
    assert statuses(org)["ACC-2"] == "fail"
