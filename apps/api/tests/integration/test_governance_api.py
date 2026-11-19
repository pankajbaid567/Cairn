"""Governance through the API: decisions, approvals, policies, attestations, risks."""

from __future__ import annotations

import pytest

from .saas_helpers import Workspace
from .test_saas_api import Saas, saas  # noqa: F401  (the fixture)

pytestmark = pytest.mark.integration

ROLES = {"adm": "admin", "mgr": "manager", "mgr2": "manager", "mem": "member", "aud": "auditor"}


def test_a_decision_goes_from_proposed_to_implemented_with_a_different_approver(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    r = w["mem"].post(
        "/governance/decisions",
        json={
            "title": "Adopt PostgreSQL",
            "question": "Which DB?",
            "outcome": "Use PostgreSQL 18",
            "required": 1,
        },
    )
    assert r.status_code == 201, r.text
    key, approval = r.json()["key"], r.json()["approval_id"]
    listed = w["mem"].get("/governance/decisions").json()["decisions"]
    assert (listed[0]["key"], listed[0]["status"], listed[0]["approval_state"]) == (
        key,
        "proposed",
        "pending",
    )
    assert (
        w["mem"]
        .post(f"/governance/approvals/{approval}/vote", json={"vote": "approve"})
        .status_code
        == 403
    )  # a member cannot vote
    waiting = w["mgr"].get("/governance/approvals").json()["approvals"][0]
    assert waiting["can_vote"] is True and waiting["requested_by"] == "Mem"  # noqa: PT018
    assert w["mgr"].post(
        f"/governance/approvals/{approval}/vote", json={"vote": "approve", "comment": "agreed"}
    ).json() == {"state": "approved"}
    assert w["mem"].get("/governance/decisions").json()["decisions"][0]["status"] == "accepted"
    assert (
        w["mem"]
        .post(
            f"/governance/decisions/{listed[0]['decision_id']}/implemented", json={"note": "done"}
        )
        .status_code
        == 403
    )
    assert (
        w["mgr"]
        .post(
            f"/governance/decisions/{listed[0]['decision_id']}/implemented", json={"note": "done"}
        )
        .status_code
        == 200
    )
    detail = w["mgr"].get(f"/governance/approvals/{approval}").json()
    assert detail["votes"][0]["voter"] == "Mgr" and detail["approval"]["state"] == "approved"  # noqa: PT018
    actions = {e["action"] for e in w.owner.get("/org/audit").json()["events"]}
    assert {"decision.proposed", "approval.approved", "decision.implemented"} <= actions


def test_nobody_approves_their_own_request_and_the_attempt_is_audited(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    a = (
        w["mgr"]
        .post(
            "/governance/decisions",
            json={"title": "Move to Kafka", "outcome": "Adopt Kafka for events"},
        )
        .json()["approval_id"]
    )
    r = w["mgr"].post(f"/governance/approvals/{a}/vote", json={"vote": "approve"})
    assert r.status_code == 403 and "segregation" in r.json()["reason"]  # noqa: PT018
    denied = [e for e in w.owner.get("/org/audit").json()["events"] if e["outcome"] == "denied"]
    assert any(e["action"] == "approval.vote" for e in denied)
    assert w["mgr2"].post(
        f"/governance/approvals/{a}/vote", json={"vote": "reject", "comment": "later"}
    ).json() == {"state": "rejected"}
    assert (
        w["adm"].post(f"/governance/approvals/{a}/vote", json={"vote": "approve"}).status_code
        == 409
    )  # already decided


def test_a_request_needing_more_approvers_than_exist_is_refused(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, {"mem": "member"})  # only the owner can approve
    r = w["mem"].post(
        "/governance/decisions",
        json={"title": "Big change", "outcome": "Rebuild everything", "required": 3},
    )
    assert r.status_code == 409 and "not enough approvers" in r.json()["reason"]  # noqa: PT018


def test_an_access_request_is_granted_only_by_an_admin(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    r = w["mem"].post(
        "/governance/approvals",
        json={
            "kind": "access",
            "title": "Need manager access",
            "role": "manager",
            "reason": "leading Payments",
        },
    )
    assert r.status_code == 201, r.text
    a = r.json()["approval_id"]
    assert w["mgr"].get(f"/governance/approvals/{a}").json()["approval"]["can_vote"] is False
    assert (
        w["mgr"].post(f"/governance/approvals/{a}/vote", json={"vote": "approve"}).status_code
        == 403
    )
    assert w["adm"].post(f"/governance/approvals/{a}/vote", json={"vote": "approve"}).json() == {
        "state": "approved"
    }
    assert w["mem"].get("/session").json()["role"] == "manager"
    assert (
        w["mem"]
        .post(
            "/governance/approvals", json={"kind": "access", "title": "Be owner", "role": "owner"}
        )
        .status_code
        == 422
    )


def test_policy_lifecycle_with_attestations(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    assert (
        w["mem"]
        .post(
            "/governance/policies",
            json={"title": "Acceptable Use", "body": "Use company systems responsibly."},
        )
        .status_code
        == 403
    )
    p = w["adm"].post(
        "/governance/policies",
        json={
            "title": "Access Control",
            "body": "Access is least-privilege and reviewed quarterly.",
            "attest_days": 14,
        },
    )
    assert p.status_code == 201, p.text
    pid = p.json()["policy_id"]
    assert (
        w["mem"].get("/governance/policies").json()["policies"] == []
    )  # a draft is not for members
    assert w["adm"].put(
        f"/governance/policies/{pid}/draft",
        json={
            "title": "Access Control",
            "body": "Access is least-privilege, reviewed each quarter.",
            "summary": "wording",
        },
    ).json() == {"version": 1}
    submitted = w["adm"].post(f"/governance/policies/{pid}/submit")
    assert submitted.status_code == 200
    a = submitted.json()["approval_id"]
    assert (
        w["adm"].post(f"/governance/approvals/{a}/vote", json={"vote": "approve"}).status_code
        == 403
    )  # SoD
    assert (
        w["adm"]
        .put(
            f"/governance/policies/{pid}/draft",
            json={"title": "x y z", "body": "changed while pending"},
        )
        .status_code
        == 409
    )
    assert w.owner.post(f"/governance/approvals/{a}/vote", json={"vote": "approve"}).json() == {
        "state": "approved"
    }
    mine = w["mem"].get("/governance/policies").json()["policies"][0]
    assert (  # noqa: PT018
        mine["state"] == "active" and mine["version"] == 1 and mine["needs_my_attestation"] is True
    )
    assert w["mem"].post(f"/governance/policies/{pid}/attest").status_code == 200
    assert (
        w["mem"].get("/governance/policies").json()["policies"][0]["needs_my_attestation"] is False
    )
    lead = w["mgr"].get(f"/governance/policies/{pid}").json()
    assert (  # noqa: PT018
        lead["attested"][0]["name"] == "Mem"
        and "Aud" not in lead["missing"]
        and "Mgr" in lead["missing"]
    )
    assert (
        w["mem"].get(f"/governance/policies/{pid}").json()["attested"] == []
    )  # members do not see who else attested
    summary = w["mgr"].get("/governance/summary").json()
    assert summary["policies_to_attest"] == 1
    # a change is a new version that must be approved again; the first stays published until then
    assert w["adm"].put(
        f"/governance/policies/{pid}/draft",
        json={
            "title": "Access Control",
            "body": "Access is least-privilege, reviewed monthly.",
            "summary": "cadence",
        },
    ).json() == {"version": 2}
    detail = w["adm"].get(f"/governance/policies/{pid}").json()
    assert [(v["version"], v["state"]) for v in detail["versions"]] == [
        (2, "draft"),
        (1, "published"),
    ]
    assert w["aud"].get(f"/governance/policies/{pid}").status_code == 200
    assert w["aud"].post(f"/governance/policies/{pid}/attest").status_code == 403


def test_the_risk_register(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    r = w["mgr"].post(
        "/governance/risks",
        json={
            "title": "Single cloud region",
            "category": "operational",
            "likelihood": 4,
            "impact": 4,
            "mitigation": "Document recovery steps",
        },
    )
    assert r.status_code == 201
    rid = r.json()["risk_id"]
    assert w["mem"].get("/governance/risks").status_code == 403
    risks = w["aud"].get("/governance/risks").json()["risks"]
    assert risks[0]["score"] == 16 and risks[0]["owner"] == "Mgr" and risks[0]["can_edit"] is False  # noqa: PT018
    assert (
        w["mgr"].patch(f"/governance/risks/{rid}", json={"state": "mitigating"}).status_code == 200
    )
    assert (
        w["mgr2"].patch(f"/governance/risks/{rid}", json={"state": "closed"}).status_code == 404
    )  # not the owner
    assert w["mgr"].patch(f"/governance/risks/{rid}", json={}).status_code == 422
    assert w["adm"].get("/governance/summary").json()["high_risks"] == 1


def test_governance_is_isolated_between_organisations(saas: Saas) -> None:  # noqa: F811
    a = Workspace(saas, {"mem": "member", "mgr": "manager"})
    b = Workspace(saas, {"mem": "member", "mgr": "manager"})
    a["mem"].post(
        "/governance/decisions", json={"title": "A only decision", "outcome": "Only in A"}
    )
    assert b["mgr"].get("/governance/approvals").json()["approvals"] == []
    assert b["mem"].get("/governance/decisions").json()["decisions"] == []
