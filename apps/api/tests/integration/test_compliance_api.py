"""Access reviews, controls, anchors and the evidence pack through the API."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from collections.abc import Iterator
from typing import cast

import pytest
from fastapi import FastAPI

from cairn_api.anchors import MemoryAnchorStore

from .saas_helpers import Workspace
from .test_saas_api import Saas, saas  # noqa: F401  (the fixture)

pytestmark = pytest.mark.integration

ROLES = {"adm": "admin", "mgr": "manager", "mem": "member", "aud": "auditor"}


@pytest.fixture
def anchors(saas: Saas) -> Iterator[MemoryAnchorStore]:  # noqa: F811
    rt = cast(FastAPI, saas.main.app).state.runtime
    store = MemoryAnchorStore()
    old, rt.anchor_store = rt.anchor_store, store
    yield store
    rt.anchor_store = old


def test_an_access_review_from_start_to_close(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    assert (
        w["mem"].post("/compliance/reviews", json={"title": "Q4 access review"}).status_code == 403
    )
    assert (
        w["mgr"].post("/compliance/reviews", json={"title": "Q4 access review"}).status_code == 403
    )
    r = w.owner.post("/compliance/reviews", json={"title": "Q4 access review", "due_days": 7})
    assert r.status_code == 201, r.text
    assert w.owner.post("/compliance/reviews", json={"title": "Second review"}).status_code == 409
    review = w["aud"].get("/compliance/reviews").json()["reviews"][0]
    assert review["state"] == "open" and review["total"] == 5 and review["decided"] == 0  # noqa: PT018
    assert all(i["reviewer"] != i["member"] for i in review["items"])
    mem_item = next(i for i in review["items"] if i["member"] == "Mem")
    assert (
        w.owner.post(f"/compliance/reviews/{review['review_id']}/close").status_code == 409
    )  # undecided items
    reviewer_client = (
        w.owner
        if mem_item["reviewer"] == "Gov Inc" or mem_item["reviewer"] == "Owner"
        else w["adm"]
    )
    other_client = w["adm"] if reviewer_client is w.owner else w.owner
    assert (
        other_client.post(
            f"/compliance/reviews/items/{mem_item['item_id']}/decide", json={"decision": "remove"}
        ).status_code
        == 403
    )
    assert (
        reviewer_client.post(
            f"/compliance/reviews/items/{mem_item['item_id']}/decide",
            json={"decision": "remove", "comment": "left"},
        ).status_code
        == 200
    )
    assert w["mem"].get("/org/members").status_code == 409  # removed: their access ended at once
    # everyone else is kept, then the review closes
    for client in (w.owner, w["adm"]):
        for i in client.get("/compliance/reviews").json()["reviews"][0]["items"]:
            if i["mine"] and i["decision"] == "pending":
                assert (
                    client.post(
                        f"/compliance/reviews/items/{i['item_id']}/decide",
                        json={"decision": "keep"},
                    ).status_code
                    == 200
                )
    assert w.owner.post(f"/compliance/reviews/{review['review_id']}/close").status_code == 200
    assert w.owner.get("/compliance/reviews").json()["reviews"][0]["state"] == "closed"
    actions = {e["action"] for e in w.owner.get("/org/audit").json()["events"]}
    assert {"review.started", "review.decided", "review.closed"} <= actions


def test_controls_are_for_admins_and_auditors_and_report_evidence(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    assert w["mem"].get("/compliance/controls").status_code == 403
    assert w["mgr"].get("/compliance/controls").status_code == 403
    first = w["aud"].get("/compliance/controls").json()
    by_id = {c["id"]: c["status"] for c in first["controls"]}
    assert len(first["controls"]) >= 15 and "certification" in first["note"]  # noqa: PT018
    assert (  # noqa: PT018
        by_id["POL-1"] == "no_evidence"
        and by_id["ACC-1"] == "no_evidence"
        and by_id["LOG-1"] == "pass"
    )
    p = (
        w["adm"]
        .post(
            "/governance/policies",
            json={"title": "Access Control", "body": "Least privilege applies to everyone."},
        )
        .json()["policy_id"]
    )
    a = w["adm"].post(f"/governance/policies/{p}/submit").json()["approval_id"]
    w.owner.post(f"/governance/approvals/{a}/vote", json={"vote": "approve"})
    second = {c["id"]: c["status"] for c in w["aud"].get("/compliance/controls").json()["controls"]}
    assert second["POL-1"] == "pass" and second["CHG-1"] == "pass"  # noqa: PT018


def test_the_evidence_pack_is_a_signed_zip_whose_hashes_match(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    w["mem"].post(
        "/governance/decisions", json={"title": "Adopt PostgreSQL", "outcome": "Use PostgreSQL 18"}
    )
    assert w["mem"].get("/compliance/evidence-pack").status_code == 403
    r = w["aud"].get("/compliance/evidence-pack")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"  # noqa: PT018
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {
        "manifest.json",
        "manifest.sig",
        "controls.csv",
        "audit_events.csv",
        "approvals.csv",
        "members.csv",
        "chain_verification.json",
    } <= names
    manifest = json.loads(z.read("manifest.json"))
    for name, meta in manifest["files"].items():
        assert hashlib.sha256(z.read(name)).hexdigest() == meta["sha256"], name
    rt = cast(FastAPI, saas.main.app).state.runtime
    assert (
        z.read("manifest.sig").decode().strip()
        == hmac.new(rt.audit_key, z.read("manifest.json"), hashlib.sha256).hexdigest()
    )
    assert json.loads(z.read("chain_verification.json"))["ok"] is True
    assert "Adopt PostgreSQL" in z.read("approvals.csv").decode()
    assert "evidence.exported" in {e["action"] for e in w.owner.get("/org/audit").json()["events"]}


def test_anchors_catch_a_chain_that_was_rewritten_end_to_end(
    saas: Saas,
    anchors: MemoryAnchorStore,  # noqa: F811
) -> None:
    w = Workspace(saas, ROLES)
    for i in range(3):
        w.owner.post("/org/teams", json={"name": f"Team {i} anchor"})
    assert w["mem"].post("/org/audit/anchor").status_code == 403
    r = w["aud"].post("/org/audit/anchor")
    assert r.status_code == 201, r.text
    seq = r.json()["seq"]
    listed = w["aud"].get("/org/audit/anchors").json()
    assert (  # noqa: PT018
        listed["ok"] is True
        and listed["anchors"][0]["status"] == "match"
        and listed["anchors"][0]["seq"] == seq
    )
    # An attacker with full database access rewrites an anchored event and recomputes every hash after it.  # noqa: E501
    org = w.owner.get("/org").json()["org_id"]
    with saas.db.connect_superuser() as su:
        su.execute("SET session_replication_role = replica")
        rows = su.execute(
            "SELECT org_seq, at, actor_principal, actor_account, actor_role, action, target_type, target_id, detail, outcome FROM cairn.audit_events WHERE org_id = %s ORDER BY org_seq",  # noqa: E501
            (org,),
        ).fetchall()
        prev = "0" * 64
        for r_ in rows:
            target = "rewritten" if r_[0] == 2 else r_[7]
            h = su.execute(
                "SELECT cairn.audit_row_hash(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",  # noqa: E501
                (
                    prev,
                    org,
                    r_[0],
                    r_[1],
                    r_[2],
                    r_[3],
                    r_[4],
                    r_[5],
                    r_[6],
                    target,
                    json.dumps(r_[8]),
                    r_[9],
                ),
            ).fetchone()
            assert h
            su.execute(
                "UPDATE cairn.audit_events SET target_id = %s, prev_hash = %s, row_hash = %s WHERE org_id = %s AND org_seq = %s",  # noqa: E501
                (target, prev, h[0], org, r_[0]),
            )
            prev = h[0]
    assert w["aud"].get("/org/audit/verify").json()["ok"] is True  # the chain alone cannot tell
    after = w["aud"].get("/org/audit/anchors").json()
    assert after["ok"] is False and after["anchors"][0]["status"] == "mismatch"  # noqa: PT018


def test_anchors_are_off_without_a_store(saas: Saas) -> None:  # noqa: F811
    w = Workspace(saas, ROLES)
    rt = cast(FastAPI, saas.main.app).state.runtime
    old, rt.anchor_store = rt.anchor_store, None
    try:
        assert w["aud"].post("/org/audit/anchor").status_code == 404
    finally:
        rt.anchor_store = old
