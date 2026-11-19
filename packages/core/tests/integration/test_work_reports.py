"""Who may read a daily report, and the rules for writing one (migration 0016)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import psycopg
import pytest

from cairn_core.testing import FreshDb

pytestmark = pytest.mark.integration


def owner(db: FreshDb) -> psycopg.Connection[Any]:
    return db.connect_owner()


def app(db: FreshDb, org: int, account: str, role: str, pid: int) -> psycopg.Connection[Any]:
    conn = psycopg.connect(db.app_dsn, autocommit=False, prepare_threshold=None)
    for name, value in (
        ("cairn.org_id", str(org)),
        ("cairn.account_id", account),
        ("cairn.role", role),
        ("cairn.principal_id", str(pid)),
    ):
        conn.execute("SELECT set_config(%s, %s, false)", (name, value))
    conn.commit()
    return conn


class World:
    """An organisation with an owner, two teams and members: p1,p2 in team T1; p3 in T2; one auditor."""

    def __init__(self, db: FreshDb) -> None:
        self.db = db
        tag = uuid.uuid4().hex[:6]
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
            self.owner = (
                str(acct[0]),
                int(
                    o.execute(
                        "SELECT principal_id FROM cairn_meta.org_members WHERE org_id=%s AND account_id=%s",
                        (self.org, acct[0]),
                    ).fetchone()[0]
                ),  # type: ignore[index]
            )
            self.people: dict[str, tuple[str, int]] = {}
            for name, role in (
                ("p1", "member"),
                ("p2", "manager"),
                ("p3", "member"),
                ("aud", "auditor"),
                ("gst", "guest"),
            ):
                a = o.execute(
                    "SELECT cairn_meta.upsert_account(%s, %s, %s)",
                    (f"s{name}{tag}", f"{name}{tag}@x.example", name),
                ).fetchone()
                assert a
                pid = o.execute(
                    "SELECT cairn_meta.add_member_internal(%s, %s, %s)", (self.org, a[0], role)
                ).fetchone()
                assert pid
                self.people[name] = (str(a[0]), int(pid[0]))
            self.roles = {
                "p1": "member",
                "p2": "manager",
                "p3": "member",
                "aud": "auditor",
                "gst": "guest",
            }
        with app(db, self.org, *self.owner_ctx()) as c:
            t1 = c.execute("SELECT cairn_meta.create_team('t1', 'T1')").fetchone()
            t2 = c.execute("SELECT cairn_meta.create_team('t2', 'T2')").fetchone()
            assert t1 and t2
            for name, team in (("p1", t1[0]), ("p2", t1[0]), ("p3", t2[0])):
                c.execute(
                    "SELECT cairn_meta.team_set_member(%s, %s, true)", (team, self.people[name][1])
                )
            c.commit()

    def owner_ctx(self) -> tuple[str, str, int]:
        return self.owner[0], "owner", self.owner[1]

    def as_(self, name: str) -> psycopg.Connection[Any]:
        acct, pid = self.people[name]
        return app(self.db, self.org, acct, self.roles[name], pid)

    def write(self, name: str, text: str = "shipped it", day: dt.date | None = None) -> None:
        with self.as_(name) as c:
            c.execute(
                "INSERT INTO cairn.work_reports (author_principal, report_date, done) VALUES (%s, %s, %s)",
                (self.people[name][1], day or dt.date.today(), text),
            )
            c.commit()


def visible_to(w: World, name: str) -> set[str]:
    with w.as_(name) as c:
        return authors(w, c)


def authors(w: World, conn: psycopg.Connection[Any]) -> set[str]:
    pid_to_name = {pid: n for n, (_, pid) in w.people.items()} | {w.owner[1]: "owner"}
    return {
        pid_to_name[r[0]] for r in conn.execute("SELECT author_principal FROM cairn.work_reports")
    }


def test_visibility_follows_role_and_team(fresh_db: FreshDb) -> None:
    w = World(fresh_db)
    for n in ("p1", "p2", "p3"):
        w.write(n)
    with app(fresh_db, w.org, *w.owner_ctx()) as c:
        c.execute(
            "INSERT INTO cairn.work_reports (author_principal, report_date, done) VALUES (%s, current_date, 'owner work')",
            (w.owner[1],),
        )
        assert authors(w, c) == {"p1", "p2", "p3", "owner"}  # an owner reads everyone's
    assert visible_to(w, "p1") == {"p1", "p2"}  # a member: self and teammates
    assert visible_to(w, "p2") == {"p1", "p2"}  # a manager: their team
    assert visible_to(w, "p3") == {"p3"}  # another team: only their own
    assert visible_to(w, "aud") == set()  # an auditor reads no report
    assert visible_to(w, "gst") == set()


def test_a_report_is_written_only_by_its_author_for_a_recent_day(fresh_db: FreshDb) -> None:
    w = World(fresh_db)
    with w.as_("p1") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO cairn.work_reports (author_principal, report_date, done) VALUES (%s, current_date, 'x')",
                (w.people["p3"][1],),
            )
        c.rollback()
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            c.execute(
                "INSERT INTO cairn.work_reports (author_principal, report_date, done) VALUES (%s, current_date + 1, 'x')",
                (w.people["p1"][1],),
            )
        c.rollback()
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            c.execute(
                "INSERT INTO cairn.work_reports (author_principal, report_date, done) VALUES (%s, current_date - 15, 'x')",
                (w.people["p1"][1],),
            )
        c.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "INSERT INTO cairn.work_reports (author_principal, report_date, done, blockers) VALUES (%s, current_date, ' ', '')",
                (w.people["p1"][1],),
            )
    with w.as_("aud") as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(
            "INSERT INTO cairn.work_reports (author_principal, report_date, done) VALUES (%s, current_date, 'x')",
            (w.people["aud"][1],),
        )


def test_one_report_per_day_can_be_edited_by_its_author_but_not_deleted_or_reassigned(
    fresh_db: FreshDb,
) -> None:
    w = World(fresh_db)
    w.write("p1")
    with w.as_("p1") as c:
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute(
                "INSERT INTO cairn.work_reports (author_principal, report_date, done) VALUES (%s, current_date, 'again')",
                (w.people["p1"][1],),
            )
        c.rollback()
        assert (
            c.execute(
                "UPDATE cairn.work_reports SET done = 'edited' WHERE author_principal = %s",
                (w.people["p1"][1],),
            ).rowcount
            == 1
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE cairn.work_reports SET author_principal = %s", (w.people["p3"][1],))
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM cairn.work_reports")
    with w.as_("p2") as c:  # a teammate and manager reads it but cannot change it
        assert c.execute("UPDATE cairn.work_reports SET done = 'hijacked'").rowcount == 0


def test_comments_are_visible_exactly_when_the_report_is(fresh_db: FreshDb) -> None:
    w = World(fresh_db)
    w.write("p1")
    with w.as_("p2") as c:
        rid = c.execute("SELECT report_id FROM cairn.work_reports").fetchone()
        assert rid
        c.execute(
            "INSERT INTO cairn.report_comments (report_id, author_principal, body) VALUES (%s, %s, 'nice')",
            (rid[0], w.people["p2"][1]),
        )
        c.commit()
    with w.as_("p1") as c:
        assert c.execute("SELECT count(*) FROM cairn.report_comments").fetchone() == (1,)
    with w.as_("p3") as c:  # not a teammate: cannot see the report, so not the comment, nor add one
        assert c.execute("SELECT count(*) FROM cairn.report_comments").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO cairn.report_comments (report_id, author_principal, body) VALUES (%s, %s, 'hi')",
                (rid[0], w.people["p3"][1]),
            )


def test_another_organisation_never_sees_the_reports(fresh_db: FreshDb) -> None:
    w1, w2 = World(fresh_db), World(fresh_db)
    w1.write("p1")
    w2.write("p1")
    with w1.as_("p1") as c:
        assert {r[0] for r in c.execute("SELECT DISTINCT org_id FROM cairn.work_reports")} == {
            w1.org
        }
    with owner(fresh_db) as o:
        assert o.execute(
            "SELECT count(DISTINCT org_id) FROM cairn.work_reports WHERE org_id = ANY(%s)",
            ([w1.org, w2.org],),
        ).fetchone() == (2,)
