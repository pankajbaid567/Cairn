"""Daily reports through the API: who sees what, edit windows, roll-ups, digests, export."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn_retrieval.llm.types import LlmRequest, LlmResponse

from .test_saas_api import PASSWORD, Saas, saas  # noqa: F401  (the fixture)

pytestmark = pytest.mark.integration

TODAY = dt.datetime.now(dt.UTC).date()


class Org:
    """An owner plus a manager and a member in team Payments, and a member in team Mobile."""

    def __init__(self, saas: Saas) -> None:
        self.saas = saas
        self.owner, _ = saas.with_org("owner", "Reports Inc")
        self.members: dict[str, TestClient] = {}
        self.pids: dict[str, int] = {}
        pay = self.owner.post("/org/teams", json={"name": "Payments"}).json()["principal_id"]
        mob = self.owner.post("/org/teams", json={"name": "Mobile"}).json()["principal_id"]
        for name, role, team in (
            ("mia", "manager", pay),
            ("max", "member", pay),
            ("zed", "member", mob),
        ):
            self.join(name, role, team)

    def join(self, name: str, role: str, team: int) -> None:
        mail = self.saas.email(name)
        token = self.owner.post("/org/invites", json={"email": mail, "role": role}).json()["token"]
        c = self.saas.new_client()
        c.post("/auth/signup", json={"email": mail, "password": PASSWORD, "name": name.title()})
        c.post("/auth/confirm", json={"email": mail, "code": "123456"})
        c.post("/auth/login", json={"email": mail, "password": PASSWORD})
        assert c.post("/invites/accept", json={"token": token}).status_code == 200
        pid = next(
            m["principal_id"]
            for m in self.owner.get("/org/members").json()["members"]
            if m["email"] == mail
        )
        assert (
            self.owner.post(
                f"/org/teams/{team}/members", json={"principal_id": pid, "on": True}
            ).status_code
            == 200
        )
        self.members[name], self.pids[name] = c, pid


def report(c: TestClient, day: dt.date = TODAY, **kw: Any) -> Any:
    body = {"done": "Shipped the refund fix", "next": "Review", "blockers": "", "links": [], **kw}
    return c.put(f"/reports/{day.isoformat()}", json=body)


def test_submit_edit_and_read_back(saas: Saas) -> None:
    org = Org(saas)
    r = report(
        org.members["max"], links=[{"kind": "decision", "ref": "ADR-007", "title": "Postgres"}]
    )
    assert r.status_code == 201 and r.json()["created"] is True
    again = report(
        org.members["max"],
        done="Shipped the refund fix and wrote tests",
        links=[{"kind": "decision", "ref": "ADR-007", "title": "Postgres"}],
    )
    assert again.status_code == 200 and again.json()["created"] is False
    mine = org.members["max"].get("/reports/mine").json()
    assert mine["editable"] is True
    assert (
        mine["report"]["done"].endswith("wrote tests")
        and mine["report"]["links"][0]["ref"] == "ADR-007"
    )
    actions = [e["action"] for e in org.owner.get("/org/audit").json()["events"]]
    assert "report.submitted" in actions and "report.updated" in actions


def test_dates_and_content_are_validated(saas: Saas) -> None:
    c = Org(saas).members["max"]
    assert report(c, TODAY + dt.timedelta(days=1)).status_code == 422
    assert report(c, TODAY - dt.timedelta(days=15)).status_code == 422
    assert report(c, done="  ", blockers="").status_code == 422
    assert c.put("/reports/not-a-date", json={"done": "x"}).status_code == 422
    assert report(c, TODAY - dt.timedelta(days=3)).status_code == 201  # inside the 14-day window


def test_visibility_by_role_and_team(saas: Saas) -> None:
    org = Org(saas)
    for name in ("mia", "max", "zed"):
        report(org.members[name], done=f"{name} work")
    names = lambda c: {r["author"] for r in c.get("/reports").json()["reports"]}  # noqa: E731
    assert names(org.members["max"]) == {"Mia", "Max"}  # a member: self and team
    assert names(org.members["mia"]) == {"Mia", "Max"}  # a manager: their team
    assert names(org.members["zed"]) == {"Zed"}
    assert names(org.owner) == {"Mia", "Max", "Zed"} or names(org.owner) >= {"Mia", "Max", "Zed"}
    zed_report = next(
        r for r in org.owner.get("/reports").json()["reports"] if r["author"] == "Zed"
    )
    assert org.members["max"].get(f"/reports/{zed_report['report_id']}").status_code == 404
    assert org.owner.get(f"/reports/{zed_report['report_id']}").status_code == 200


def test_comments_follow_report_visibility(saas: Saas) -> None:
    org = Org(saas)
    report(org.members["max"])
    rid = org.members["max"].get("/reports").json()["reports"][0]["report_id"]
    assert (
        org.members["mia"].post(f"/reports/{rid}/comments", json={"body": "Nice work"}).status_code
        == 201
    )
    detail = org.members["max"].get(f"/reports/{rid}").json()
    assert [c["body"] for c in detail["comments"]] == ["Nice work"]
    assert (
        org.members["zed"].post(f"/reports/{rid}/comments", json={"body": "hi"}).status_code == 404
    )


def test_rollup_is_for_leads_and_counts_who_is_missing(saas: Saas) -> None:
    org = Org(saas)
    yesterday = TODAY - dt.timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= dt.timedelta(days=1)
    report(org.members["max"], yesterday, blockers="Waiting on the ledger team")
    assert org.members["max"].get("/reports/rollup").status_code == 403
    r = (
        org.members["mia"]
        .get(
            "/reports/rollup", params={"start": yesterday.isoformat(), "end": yesterday.isoformat()}
        )
        .json()
    )
    day = r["days"][0]
    assert (
        r["people"] == 2
        and day["submitted"] == 1
        and day["missing"] == ["Mia"]
        and r["with_blockers"] == 1
    )
    owner_view = org.owner.get(
        "/reports/rollup", params={"start": yesterday.isoformat(), "end": yesterday.isoformat()}
    ).json()
    assert owner_view["people"] == 4 and "Zed" in owner_view["days"][0]["missing"]


class StubLlm:
    provider, model = "stub", "stub-1"

    def __init__(self, reply: str) -> None:
        self.reply, self.prompts = reply, []  # type: ignore[var-annotated]

    async def complete(self, req: LlmRequest) -> LlmResponse:
        self.prompts.append(req.user)
        return LlmResponse(
            text=self.reply,
            tokens_in=1,
            tokens_out=1,
            latency_ms=1.0,
            provider="stub",
            model="stub-1",
        )


def test_digest_cites_real_reports_only_and_is_cached(saas: Saas) -> None:
    org = Org(saas)
    report(org.members["max"], done="Fixed the refund event bug")
    report(org.members["mia"], done="Reviewed the ledger design", blockers="Needs an ADR approval")
    reports = org.members["mia"].get("/reports").json()["reports"]
    ids = [r["report_id"] for r in reports]
    reply = json.dumps(
        {
            "summary": "The team fixed a refund bug and reviewed the ledger design.",
            "highlights": [
                {"text": "Refund event bug fixed", "reports": [f"r{ids[0]}", "r999999"]},
                {"text": "Invented work", "reports": ["r999999"]},
            ],
            "blockers": [{"text": "An ADR approval is needed", "reports": [f"r{ids[1]}"]}],
        }
    )
    stub = StubLlm(reply)
    providers = saas.main.app.state.runtime.providers
    old = providers.answer
    object.__setattr__(providers, "answer", stub)
    try:
        body = {
            "start": (TODAY - dt.timedelta(days=2)).isoformat(),
            "end": TODAY.isoformat(),
            "scope": "team",
        }
        r = org.members["mia"].post("/reports/digest", json=body)
        assert r.status_code == 200, r.text
        d = r.json()
        assert len(d["highlights"]) == 1 and d["highlights"][0]["reports"] == [
            ids[0]
        ]  # the invented item is dropped
        assert d["blockers"][0]["reports"] == [ids[1]] and d["cached"] is False
        assert "Fixed the refund event bug" in stub.prompts[0]
        again = org.members["mia"].post("/reports/digest", json=body).json()
        assert again["cached"] is True and len(stub.prompts) == 1
        assert (
            org.members["max"].post("/reports/digest", json={**body, "scope": "org"}).status_code
            == 403
        )
        # an unusable reply is an error, not a made-up digest
        stub.reply = "I could not do that"
        report(org.members["max"], done="A second report changes the input")
        assert org.members["mia"].post("/reports/digest", json=body).status_code == 503
    finally:
        object.__setattr__(providers, "answer", old)


def test_export_is_csv_for_leads_only(saas: Saas) -> None:
    org = Org(saas)
    report(org.members["max"], done='Wrote, "quoted" text')
    assert org.members["max"].get("/reports/export").status_code == 403
    r = org.members["mia"].get("/reports/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "Wrote" in r.text and r.text.splitlines()[0].startswith("date,author")


def test_a_guest_and_an_auditor_cannot_write_or_read_reports(saas: Saas) -> None:
    org = Org(saas)
    report(org.members["max"])
    for role in ("auditor", "guest"):
        mail = saas.email(role)
        token = org.owner.post("/org/invites", json={"email": mail, "role": role}).json()["token"]
        c = saas.new_client()
        c.post("/auth/signup", json={"email": mail, "password": PASSWORD, "name": role})
        c.post("/auth/confirm", json={"email": mail, "code": "123456"})
        c.post("/auth/login", json={"email": mail, "password": PASSWORD})
        c.post("/invites/accept", json={"token": token})
        assert c.get("/reports").json()["reports"] == []
        assert report(c).status_code == 403
        assert uuid.uuid4()  # keep the import used
