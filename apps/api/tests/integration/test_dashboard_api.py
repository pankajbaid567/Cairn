"""The dashboard shows each role only what concerns it."""

from __future__ import annotations

import datetime as dt

import pytest

from .saas_helpers import Workspace
from .test_saas_api import Saas, saas  # noqa: F401  (the fixture)

pytestmark = pytest.mark.integration

ROLES = {"adm": "admin", "mgr": "manager", "mem": "member", "aud": "auditor", "gst": "guest"}
TODAY = dt.datetime.now(dt.UTC).date()


def test_each_role_sees_its_own_slice(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    owner = w.owner.get("/dashboard").json()
    assert (  # noqa: PT018
        owner["audit"]["chain_ok"] is True
        and owner["controls"]["pass"] >= 1
        and owner["members"] == 6
    )
    assert "team_today" in owner and "risks" in owner  # noqa: PT018
    member = w["mem"].get("/dashboard").json()
    assert "audit" not in member and "team_today" not in member and "risks" not in member  # noqa: PT018
    assert member["my_report_today"] is False and member["policies_to_attest"] == 0  # noqa: PT018
    auditor = w["aud"].get("/dashboard").json()
    assert "audit" in auditor and "my_report_today" not in auditor and auditor["risks"]["open"] == 0  # noqa: PT018
    guest = w["gst"].get("/dashboard").json()
    assert (  # noqa: PT018
        set(guest) >= {"role", "members", "recent"}
        and "audit" not in guest
        and "my_report_today" not in guest
    )


def test_the_numbers_follow_what_people_do(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    w["mem"].put(
        f"/reports/{TODAY.isoformat()}",
        json={"done": "Shipped", "next": "", "blockers": "Waiting on ledger", "links": []},
    )
    a = (
        w["mem"]
        .post(
            "/governance/decisions",
            json={"title": "Adopt PostgreSQL", "outcome": "Use PostgreSQL 18"},
        )
        .json()["approval_id"]
    )
    assert w["mem"].get("/dashboard").json()["my_report_today"] is True
    assert w["mem"].get("/dashboard").json()["my_requests_pending"] == 1
    mgr = w["mgr"].get("/dashboard").json()
    assert mgr["approvals_waiting"] == 1
    w["mgr"].post(f"/governance/approvals/{a}/vote", json={"vote": "approve"})
    assert w["mgr"].get("/dashboard").json()["approvals_waiting"] == 0
    assert w.owner.get("/dashboard").json()["team_today"]["submitted"] == 1
    assert w.owner.get("/dashboard").json()["recent"][0]["action"].startswith("approval.")
