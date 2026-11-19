"""Access reviews, controls, audit anchors and the evidence pack (SaaS plan, Phase D)."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import zipfile
from typing import Any

import psycopg
from fastapi import APIRouter, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from cairn_api.anchors import Anchor, sign_anchor, signature_ok
from cairn_api.apihelp import error, json_response
from cairn_api.people import AUDIT_READERS, _db_rule, guard
from cairn_api.tenant import Ctx, record, scope_of

ADMIN = ("owner", "admin")
REVIEWERS = ("owner", "admin")


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=120)
    due_days: int = Field(14, ge=1, le=90)


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(keep|change|remove)$")
    new_role: str | None = Field(None, pattern="^(owner|admin|manager|member|auditor|guest)$")
    comment: str = Field("", max_length=500)


def csv_bytes(header: list[str], rows: list[Any]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    for r in rows:
        w.writerow(
            ["" if v is None else (v.isoformat() if hasattr(v, "isoformat") else v) for v in r]
        )
    return out.getvalue().encode()


def install(app: FastAPI, rt: Any) -> None:
    router = APIRouter()

    async def head(ctx: Ctx, conn: Any) -> Any:
        return await (await conn.execute("SELECT * FROM cairn.verify_chain()")).fetchone()

    # --- access reviews ------------------------------------------------------------------

    @router.get("/compliance/reviews")
    async def reviews(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "review.list", ("owner", "admin", "manager", "auditor"))
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            heads = await (
                await conn.execute(
                    "SELECT r.review_id, r.title, r.state, r.started_at, r.due_at, r.closed_at, p.display_name, "  # noqa: E501
                    "(SELECT count(*) FROM cairn.access_review_items i WHERE i.review_id = r.review_id), "  # noqa: E501
                    "(SELECT count(*) FROM cairn.access_review_items i WHERE i.review_id = r.review_id AND i.decision <> 'pending') "  # noqa: E501
                    "FROM cairn.access_reviews r JOIN cairn.principals p ON p.principal_id = r.started_by "  # noqa: E501
                    "ORDER BY r.review_id DESC LIMIT 20"
                )
            ).fetchall()
            items = await (
                await conn.execute(
                    "SELECT i.item_id, i.review_id, i.member_name, i.role_at_start, i.teams_at_start, rv.display_name, "  # noqa: E501
                    "i.decision, i.new_role, i.comment, i.decided_at, i.reviewer_principal = %s "
                    "FROM cairn.access_review_items i JOIN cairn.principals rv ON rv.principal_id = i.reviewer_principal "  # noqa: E501
                    "ORDER BY i.review_id DESC, i.member_name",
                    (ctx.principal_id,),
                )
            ).fetchall()
        by_review: dict[int, list[dict[str, Any]]] = {}
        for i in items:
            by_review.setdefault(int(i[1]), []).append(
                {
                    "item_id": i[0],
                    "member": i[2],
                    "role": i[3],
                    "teams": list(i[4]),
                    "reviewer": i[5],
                    "decision": i[6],
                    "new_role": i[7],
                    "comment": i[8],
                    "decided_at": i[9].isoformat() if i[9] else None,
                    "mine": bool(i[10]),
                }
            )
        now = dt.datetime.now(dt.UTC)
        return json_response(
            200,
            {
                "reviews": [
                    {
                        "review_id": h[0],
                        "title": h[1],
                        "state": h[2],
                        "started_at": h[3].isoformat(),
                        "due_at": h[4].isoformat(),
                        "closed_at": h[5].isoformat() if h[5] else None,
                        "started_by": h[6],
                        "total": h[7],
                        "decided": h[8],
                        "overdue": h[2] == "open" and h[4] < now,
                        "items": by_review.get(int(h[0]), []),
                    }
                    for h in heads
                ]
            },
        )

    @router.post("/compliance/reviews")
    async def start_review(body: ReviewBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "review.start", REVIEWERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute(
                        "SELECT cairn.start_access_review(%s, %s)", (body.title, body.due_days)
                    )
                ).fetchone()
                assert row is not None  # noqa: S101
                await record(conn, "review.started", "access_review", str(row[0]))
        except psycopg.errors.UniqueViolation:
            return json_response(409, {"error": "refused", "reason": "a review is already open"})
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(201, {"review_id": row[0]})

    @router.post("/compliance/reviews/items/{item_id}/decide")
    async def decide(item_id: int, body: DecideBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "review.decide", REVIEWERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                await conn.execute(
                    "SELECT cairn.decide_review_item(%s, %s, %s, %s)",
                    (item_id, body.decision, body.new_role, body.comment),
                )
                await record(
                    conn,
                    "review.decided",
                    "access_review_item",
                    str(item_id),
                    {"decision": body.decision},
                )
        except psycopg.errors.InsufficientPrivilege as exc:
            async with scope_of(rt.pool, ctx) as conn:
                await record(
                    conn, "review.decide", "access_review_item", str(item_id), {}, "denied"
                )
            return json_response(403, {"error": "forbidden", "reason": _db_rule(exc)})
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(200, {"status": "decided"})

    @router.post("/compliance/reviews/{review_id}/close")
    async def close(review_id: int, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "review.close", REVIEWERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                await conn.execute("SELECT cairn.close_access_review(%s)", (review_id,))
                await record(conn, "review.closed", "access_review", str(review_id))
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(200, {"status": "closed"})

    # --- controls ------------------------------------------------------------------------

    @router.get("/compliance/controls")
    async def controls(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "controls.read", AUDIT_READERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            rows = await (await conn.execute("SELECT * FROM cairn.control_status()")).fetchall()
        items = [
            {
                "id": r[0],
                "category": r[1],
                "title": r[2],
                "status": r[3],
                "detail": r[4],
                "route": r[5],
            }
            for r in rows
        ]
        counts = {
            s: sum(1 for i in items if i["status"] == s) for s in ("pass", "fail", "no_evidence")
        }
        return json_response(
            200,
            {
                "controls": items,
                "counts": counts,
                "note": "These checks map the platform's own records to control themes. They are not an audit opinion or a certification.",  # noqa: E501
            },
        )

    # --- anchors -------------------------------------------------------------------------

    def need_anchors() -> Response | None:
        return (
            None if getattr(rt, "anchor_store", None) is not None else error(404, "not_configured")
        )

    @router.post("/org/audit/anchor")
    async def anchor_now(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "audit.anchor", AUDIT_READERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        if (r := need_anchors()) is not None:
            return r
        async with scope_of(rt.pool, ctx) as conn:
            chain = await head(ctx, conn)
            if not chain or not chain[0] or not chain[3]:
                return json_response(
                    409, {"error": "refused", "reason": "the chain is empty or does not verify"}
                )
            at = dt.datetime.now(dt.UTC).isoformat()
            anchor = Anchor(
                int(ctx.org_id or 0),
                int(chain[3]),
                str(chain[4]),
                at,
                sign_anchor(rt.audit_key, int(ctx.org_id or 0), int(chain[3]), str(chain[4]), at),
            )
            await rt.anchor_store.put(anchor)
            await record(
                conn, "audit.anchored", "audit", str(anchor.seq), {"hash": anchor.head_hash[:16]}
            )
        return json_response(
            201, {"seq": anchor.seq, "head_hash": anchor.head_hash, "at": anchor.at}
        )

    @router.get("/org/audit/anchors")
    async def anchors(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "audit.anchors", AUDIT_READERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        if (r := need_anchors()) is not None:
            return r
        stored = await rt.anchor_store.list(int(ctx.org_id or 0))
        async with scope_of(rt.pool, ctx) as conn:
            hashes = {
                int(s): h
                for s, h in await (
                    await conn.execute(
                        "SELECT org_seq, row_hash FROM cairn.audit_events WHERE org_id = %s",
                        (ctx.org_id,),
                    )
                ).fetchall()
            }
        out = []
        for a in stored:
            if not signature_ok(rt.audit_key, a):
                status = "bad_signature"
            elif a.seq not in hashes:
                status = "missing_event"
            else:
                status = "match" if hashes[a.seq] == a.head_hash else "mismatch"
            out.append({"seq": a.seq, "head_hash": a.head_hash, "at": a.at, "status": status})
        return json_response(
            200,
            {
                "anchors": out[::-1],
                "ok": all(a["status"] == "match" for a in out),
                "latest_seq": max(hashes) if hashes else 0,
            },
        )

    # --- evidence pack -------------------------------------------------------------------

    @router.get("/compliance/evidence-pack")
    async def evidence_pack(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "evidence.export", AUDIT_READERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        files: dict[str, bytes] = {}
        async with scope_of(rt.pool, ctx) as conn:

            async def q(sql: str) -> list[Any]:
                return await (await conn.execute(sql)).fetchall()

            org = (
                await q(
                    f"SELECT name, slug FROM cairn_meta.orgs WHERE org_id = {int(ctx.org_id or 0)}"  # noqa: S608
                )
            )[0]
            chain = await head(ctx, conn)
            controls = await q("SELECT * FROM cairn.control_status()")
            files["controls.csv"] = csv_bytes(
                ["control", "category", "title", "status", "detail", "where"], controls
            )
            files["audit_events.csv"] = csv_bytes(
                [
                    "seq",
                    "at",
                    "action",
                    "target_type",
                    "target_id",
                    "outcome",
                    "actor_role",
                    "prev_hash",
                    "row_hash",
                ],
                await q(
                    "SELECT org_seq, at, action, target_type, target_id, outcome, actor_role, prev_hash, row_hash FROM cairn.audit_events ORDER BY org_seq"  # noqa: E501
                ),
            )
            files["members.csv"] = csv_bytes(
                ["name", "email", "role", "state", "joined_at"],
                await q(
                    "SELECT display_name, email, role, state, joined_at FROM cairn_meta.directory()"
                ),
            )
            files["approvals.csv"] = csv_bytes(
                [
                    "approval_id",
                    "kind",
                    "title",
                    "requested_by",
                    "required",
                    "state",
                    "created_at",
                    "decided_at",
                ],
                await q(
                    "SELECT a.approval_id, a.kind, a.title, p.display_name, a.required, a.state, a.created_at, a.decided_at FROM cairn.approvals a JOIN cairn.principals p ON p.principal_id = a.requested_by ORDER BY a.approval_id"  # noqa: E501
                ),
            )
            files["approval_votes.csv"] = csv_bytes(
                ["approval_id", "voter", "vote", "comment", "voted_at"],
                await q(
                    "SELECT v.approval_id, p.display_name, v.vote, v.comment, v.voted_at FROM cairn.approval_votes v JOIN cairn.principals p ON p.principal_id = v.voter_principal ORDER BY v.vote_id"  # noqa: E501
                ),
            )
            files["policies.csv"] = csv_bytes(
                [
                    "policy",
                    "version",
                    "state",
                    "published_at",
                    "attest_by",
                    "body_sha256",
                    "attested",
                ],
                await q(
                    "SELECT p.title, v.version, v.state, v.published_at, v.attest_by, encode(sha256(convert_to(v.body, 'UTF8')), 'hex'), (SELECT count(*) FROM cairn.attestations a WHERE a.version_id = v.version_id) FROM cairn.policies p JOIN cairn.policy_versions v ON v.policy_id = p.policy_id ORDER BY p.title, v.version"  # noqa: E501
                ),
            )
            files["attestations.csv"] = csv_bytes(
                ["policy", "version", "person", "attested_at"],
                await q(
                    "SELECT p.title, v.version, pr.display_name, a.attested_at FROM cairn.attestations a JOIN cairn.policy_versions v ON v.version_id = a.version_id JOIN cairn.policies p ON p.policy_id = v.policy_id JOIN cairn.principals pr ON pr.principal_id = a.principal_id ORDER BY p.title, v.version, a.attested_at"  # noqa: E501
                ),
            )
            files["access_review_items.csv"] = csv_bytes(
                [
                    "review",
                    "member",
                    "role_at_start",
                    "reviewer",
                    "decision",
                    "new_role",
                    "comment",
                    "decided_at",
                ],
                await q(
                    "SELECT r.title, i.member_name, i.role_at_start, rv.display_name, i.decision, i.new_role, i.comment, i.decided_at FROM cairn.access_review_items i JOIN cairn.access_reviews r ON r.review_id = i.review_id JOIN cairn.principals rv ON rv.principal_id = i.reviewer_principal ORDER BY r.review_id, i.member_name"  # noqa: E501
                ),
            )
            files["risks.csv"] = csv_bytes(
                [
                    "title",
                    "category",
                    "likelihood",
                    "impact",
                    "score",
                    "state",
                    "owner",
                    "mitigation",
                    "review_by",
                ],
                await q(
                    "SELECT r.title, r.category, r.likelihood, r.impact, r.score, r.state, p.display_name, r.mitigation, r.review_by FROM cairn.risks r JOIN cairn.principals p ON p.principal_id = r.owner_principal ORDER BY r.score DESC"  # noqa: E501
                ),
            )
            files["chain_verification.json"] = json.dumps(
                {
                    "ok": chain[0],
                    "events_checked": chain[1],
                    "first_bad_seq": chain[2],
                    "head_seq": chain[3],
                    "head_hash": chain[4],
                },
                indent=1,
            ).encode()
            if getattr(rt, "anchor_store", None) is not None:
                stored = await rt.anchor_store.list(int(ctx.org_id or 0))
                files["anchors.json"] = json.dumps([a.__dict__ for a in stored], indent=1).encode()
            generated = dt.datetime.now(dt.UTC).isoformat()
            manifest = {
                "organisation": {"name": org[0], "slug": org[1]},
                "generated_at": generated,
                "generated_by_role": ctx.role,
                "audit_chain": {"ok": chain[0], "head_seq": chain[3], "head_hash": chain[4]},
                "files": {
                    name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                    for name, data in sorted(files.items())
                },
                "note": "Evidence held by the platform. The controls map this evidence to themes; they are not a certification.",  # noqa: E501
            }
            manifest_bytes = json.dumps(manifest, indent=1, sort_keys=True).encode()
            signature = hmac.new(rt.audit_key, manifest_bytes, hashlib.sha256).hexdigest()
            await record(conn, "evidence.exported", "evidence", None, {"files": len(files)})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in sorted(files.items()):
                z.writestr(name, data)
            z.writestr("manifest.json", manifest_bytes)
            z.writestr("manifest.sig", signature + "\n")
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="cairn-evidence-pack.zip"',
                "Cache-Control": "no-store",
            },
        )

    app.include_router(router)
