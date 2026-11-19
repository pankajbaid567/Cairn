"""The home screen's numbers: what needs this person's attention today (SaaS plan, Phase E)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response

from cairn_api.apihelp import error, json_response
from cairn_api.people import guard
from cairn_api.tenant import scope_of

LEADS = ("owner", "admin", "manager")
READERS = ("owner", "admin", "auditor")


def install(app: FastAPI, rt: Any) -> None:
    router = APIRouter()

    @router.get("/dashboard")
    async def dashboard(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "dashboard.read")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        today = dt.datetime.now(dt.UTC).date()
        out: dict[str, Any] = {
            "role": ctx.role,
            "date": today.isoformat(),
            "weekend": today.weekday() >= 5,
        }
        async with scope_of(rt.pool, ctx) as conn:

            async def one(sql: str, *args: Any) -> Any:
                return await (await conn.execute(sql, args)).fetchone()

            writer = ctx.role in ("owner", "admin", "manager", "member")
            if writer:
                mine = await one(
                    "SELECT count(*) FROM cairn.work_reports WHERE author_principal = %s AND report_date = %s",  # noqa: E501
                    ctx.principal_id,
                    today,
                )
                out["my_report_today"] = bool(mine and mine[0])
            if ctx.role in LEADS:
                people = await one(
                    "SELECT count(*) FROM cairn_meta.directory() d WHERE d.state = 'active' "
                    "AND d.role IN ('owner', 'admin', 'manager', 'member') "
                    "AND (%s IN ('owner', 'admin') OR d.principal_id = %s OR d.principal_id IN (SELECT cairn.teammates()))",  # noqa: E501
                    ctx.role,
                    ctx.principal_id,
                )
                done = await one(
                    "SELECT count(*) FROM cairn.work_reports WHERE report_date = %s", today
                )
                blockers = await one(
                    "SELECT count(*) FROM cairn.work_reports WHERE report_date >= %s AND length(btrim(blockers)) > 0",  # noqa: E501
                    today - dt.timedelta(days=2),
                )
                out["team_today"] = {
                    "submitted": int(done[0]) if done else 0,
                    "expected": int(people[0]) if people else 0,
                    "blockers_recent": int(blockers[0]) if blockers else 0,
                }
                waiting = await one(
                    "SELECT count(*) FROM cairn.approvals a WHERE a.state = 'pending' AND a.requested_by <> %s "  # noqa: E501
                    "AND (a.kind <> 'access' OR %s IN ('owner', 'admin')) AND NOT EXISTS "
                    "(SELECT 1 FROM cairn.approval_votes v WHERE v.approval_id = a.approval_id AND v.voter_principal = %s)",  # noqa: E501
                    ctx.principal_id,
                    ctx.role,
                    ctx.principal_id,
                )
                out["approvals_waiting"] = int(waiting[0]) if waiting else 0
            mine_pending = await one(
                "SELECT count(*) FROM cairn.approvals WHERE requested_by = %s AND state = 'pending'",  # noqa: E501
                ctx.principal_id,
            )
            out["my_requests_pending"] = int(mine_pending[0]) if mine_pending else 0
            if writer:
                to_attest = await one(
                    "SELECT count(*) FROM cairn.policies p JOIN cairn.policy_versions v ON v.policy_id = p.policy_id "  # noqa: E501
                    "AND v.state = 'published' WHERE NOT EXISTS (SELECT 1 FROM cairn.attestations a "  # noqa: E501
                    "WHERE a.version_id = v.version_id AND a.principal_id = %s)",
                    ctx.principal_id,
                )
                out["policies_to_attest"] = int(to_attest[0]) if to_attest else 0
            if ctx.role in ("owner", "admin", "manager", "auditor"):
                reviews = await one(
                    "SELECT count(*) FROM cairn.access_review_items i JOIN cairn.access_reviews r ON r.review_id = i.review_id "  # noqa: E501
                    "WHERE r.state = 'open' AND i.decision = 'pending' AND i.reviewer_principal = %s",  # noqa: E501
                    ctx.principal_id,
                )
                out["review_items_mine"] = int(reviews[0]) if reviews else 0
                risks = await one(
                    "SELECT count(*) FILTER (WHERE state <> 'closed'), count(*) FILTER (WHERE state <> 'closed' AND score >= 15) FROM cairn.risks"  # noqa: E501
                )
                out["risks"] = {"open": int(risks[0]), "high": int(risks[1])} if risks else None
            if ctx.role in READERS:
                chain = await one("SELECT ok, checked, first_bad_seq FROM cairn.verify_chain()")
                events = await one(
                    "SELECT count(*) FROM cairn.audit_events WHERE at > now() - interval '24 hours'"
                )
                denied = await one(
                    "SELECT count(*) FROM cairn.audit_events WHERE outcome = 'denied' AND at > now() - interval '7 days'"  # noqa: E501
                )
                out["audit"] = {
                    "chain_ok": bool(chain and chain[0]),
                    "events": int(chain[1]) if chain else 0,
                    "last_24h": int(events[0]) if events else 0,
                    "denied_7d": int(denied[0]) if denied else 0,
                }
                controls = await conn.execute("SELECT status FROM cairn.control_status()")
                statuses = [r[0] for r in await controls.fetchall()]
                out["controls"] = {s: statuses.count(s) for s in ("pass", "fail", "no_evidence")}
            members = await one(
                "SELECT count(*) FILTER (WHERE state = 'active') FROM cairn_meta.directory()"
            )
            out["members"] = int(members[0]) if members else 0
            recent = await (
                await conn.execute(
                    "SELECT e.org_seq, e.at, e.action, e.outcome, p.display_name FROM cairn.audit_events e "  # noqa: E501
                    "LEFT JOIN cairn.principals p ON p.principal_id = e.actor_principal "
                    "WHERE e.org_id = %s ORDER BY e.org_seq DESC LIMIT 8",
                    (ctx.org_id,),
                )
            ).fetchall()
            out["recent"] = [
                {
                    "seq": r[0],
                    "at": r[1].isoformat(),
                    "action": r[2],
                    "outcome": r[3],
                    "actor": r[4],
                }
                for r in recent
            ]
        return json_response(200, out)

    app.include_router(router)
