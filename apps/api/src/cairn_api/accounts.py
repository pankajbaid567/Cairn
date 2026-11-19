"""Sign-up, sign-in, sessions and organisations (SaaS plan, Phase A)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from cairn_api.apihelp import error, json_response, limited, set_session_cookie
from cairn_api.idp import AuthError, Identity
from cairn_api.sessions import COOKIE_NAME, SESSION_COOKIE, sign_session
from cairn_api.tenant import Ctx, record, resolve_ctx, scope_of, scoped

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
SLUG = re.compile(r"[^a-z0-9]+")


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class SignUpBody(Credentials):
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=10, max_length=128)


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=4, max_length=12)


class EmailBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)


class ResetBody(ConfirmBody):
    password: str = Field(min_length=10, max_length=128)


class OrgBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=80)


class SwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: int = Field(ge=1)


def slugify(name: str) -> str:
    slug = SLUG.sub("-", name.lower()).strip("-")[:30].strip("-")
    return slug if re.fullmatch(r"[a-z][a-z0-9-]{1,38}", slug) else f"org-{slug}"[:38]


def _status_for(code: str) -> int:
    return {
        "invalid_credentials": 401,
        "not_confirmed": 403,
        "reset_required": 403,
        "weak_password": 422,
        "bad_code": 422,
        "invalid_request": 422,
        "rate_limited": 429,
    }.get(code, 503)


async def memberships(rt: Any, account_id: str) -> list[dict[str, Any]]:
    async with scoped(rt.pool, org_id=None, account_id=account_id) as conn:
        rows = await (
            await conn.execute(
                "SELECT o.org_id, o.slug, o.name, m.role, o.is_demo FROM cairn_meta.org_members m "
                "JOIN cairn_meta.orgs o ON o.org_id = m.org_id "
                "WHERE m.account_id = %s AND m.state = 'active' AND o.state = 'active' "
                "ORDER BY m.joined_at",
                (account_id,),
            )
        ).fetchall()
    return [
        {"org_id": int(r[0]), "slug": r[1], "name": r[2], "role": r[3], "is_demo": bool(r[4])}
        for r in rows
    ]


def _cookie(rt: Any, response: Response, account_id: str, org_id: int | None) -> None:
    value = sign_session(account_id, org_id, rt.secret, rt.ttl_s)
    set_session_cookie(response, SESSION_COOKIE, value, rt.ttl_s, rt.search.env == "demo")


async def _upsert(rt: Any, identity: Identity) -> str:
    async with scoped(rt.pool, org_id=None) as conn:
        row = await (
            await conn.execute(
                "SELECT cairn_meta.upsert_account(%s, %s, %s)",
                (identity.sub, identity.email, identity.name),
            )
        ).fetchone()
    assert row is not None  # noqa: S101
    return str(row[0])


async def _complete_login(rt: Any, identity: Identity) -> Response:
    account_id = await _upsert(rt, identity)
    orgs = await memberships(rt, account_id)
    org_id = orgs[0]["org_id"] if orgs else None
    response = json_response(
        200,
        {
            "account": {"id": account_id, "email": identity.email, "name": identity.name},
            "orgs": orgs,
            "org_id": org_id,
        },
    )
    _cookie(rt, response, account_id, org_id)
    if org_id is not None:
        ctx = await _ctx_for(rt, account_id, org_id)
        if ctx is not None:
            async with scope_of(rt.pool, ctx) as conn:
                await record(conn, "auth.login", "account", account_id)
    return response


async def _ctx_for(rt: Any, account_id: str, org_id: int) -> Ctx | None:
    from cairn_api.tenant import _membership

    member = await _membership(rt.pool, account_id, org_id)
    if member is None:
        return None
    pid, role, external = member
    return Ctx(account_id, org_id, pid, role, None, "", external.split(":", 1)[1])


def session_payload(
    ctx: Ctx, orgs: list[dict[str, Any]], org: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "authenticated": True,
        "account": {"id": ctx.account_id, "email": ctx.email, "name": ctx.display_name}
        if ctx.account_id
        else None,
        "persona": ctx.handle if ctx.demo_persona else None,
        "org": org,
        "role": ctx.role,
        "orgs": orgs,
        "demo": ctx.demo_persona,
        "principal_id": ctx.principal_id,
    }


def install(app: FastAPI, rt: Any) -> None:
    router = APIRouter()

    def need_idp() -> Response | None:
        return None if rt.idp is not None else error(404, "not_found")

    @router.post("/auth/signup")
    async def signup(body: SignUpBody, request: Request) -> Response:
        if (r := need_idp()) is not None:
            return r
        if (lim := limited(rt, request, "signup", 8, 600)) is not None:
            return lim
        email = body.email.strip().lower()
        if not EMAIL.match(email):
            return error(422, "invalid_request")
        try:
            confirmed = await rt.idp.sign_up(email, body.password, body.name.strip())
            if confirmed:
                return await _complete_login(rt, await rt.idp.login(email, body.password))
        except AuthError as exc:
            return error(_status_for(exc.code), exc.code)
        return json_response(202, {"status": "confirm_needed"})

    @router.post("/auth/confirm")
    async def confirm(body: ConfirmBody, request: Request) -> Response:
        if (r := need_idp()) is not None:
            return r
        if (lim := limited(rt, request, "confirm", 10, 600)) is not None:
            return lim
        try:
            await rt.idp.confirm(body.email.strip().lower(), body.code.strip())
        except AuthError as exc:
            return error(_status_for(exc.code), exc.code)
        return json_response(200, {"status": "confirmed"})

    @router.post("/auth/login")
    async def login(body: Credentials, request: Request) -> Response:
        if (r := need_idp()) is not None:
            return r
        if (lim := limited(rt, request, "login", 12, 600)) is not None:
            return lim
        try:
            identity = await rt.idp.login(body.email.strip().lower(), body.password)
        except AuthError as exc:
            return error(_status_for(exc.code), exc.code)
        return await _complete_login(rt, identity)

    @router.post("/auth/forgot")
    async def forgot(body: EmailBody, request: Request) -> Response:
        if (r := need_idp()) is not None:
            return r
        if (lim := limited(rt, request, "forgot", 5, 600)) is not None:
            return lim
        try:
            await rt.idp.forgot(body.email.strip().lower())
        except AuthError as exc:
            if exc.code == "rate_limited":
                return error(429, "rate_limited")
        return json_response(202, {"status": "sent_if_exists"})

    @router.post("/auth/reset")
    async def reset(body: ResetBody, request: Request) -> Response:
        if (r := need_idp()) is not None:
            return r
        if (lim := limited(rt, request, "reset", 8, 600)) is not None:
            return lim
        try:
            await rt.idp.reset(body.email.strip().lower(), body.code.strip(), body.password)
        except AuthError as exc:
            return error(_status_for(exc.code), exc.code)
        return json_response(200, {"status": "reset"})

    @router.get("/auth/config")
    async def auth_config() -> Response:
        """What the sign-in screen may offer."""
        return json_response(
            200,
            {
                "password_login": rt.idp is not None,
                "demo_login": bool(rt.search.dev_login_enabled),
                "auto_confirm": bool(rt.search.auth_auto_confirm),
            },
        )

    @router.get("/session")
    async def session(request: Request) -> Response:
        ctx = await resolve_ctx(rt, request)
        if ctx is None:
            return error(401, "unauthenticated")
        orgs = await memberships(rt, ctx.account_id) if ctx.account_id else []
        org = next((o for o in orgs if o["org_id"] == ctx.org_id), None)
        if org is None and ctx.org_id is not None:
            org = {
                "org_id": ctx.org_id,
                "slug": "kestrel-pay",
                "name": "Kestrel Pay (demo)",
                "role": ctx.role,
                "is_demo": True,
            }
        return json_response(200, session_payload(ctx, orgs, org))

    @router.post("/orgs")
    async def create_org(body: OrgBody, request: Request) -> Response:
        ctx = await resolve_ctx(rt, request)
        if ctx is None or ctx.account_id is None or ctx.demo_persona:
            return error(401, "unauthenticated")
        if (lim := limited(rt, request, "org_create", 5, 3600)) is not None:
            return lim
        base = slugify(body.name)
        for attempt in range(6):
            slug = base if attempt == 0 else f"{base[:30]}-{attempt + 1}"
            try:
                async with scoped(rt.pool, org_id=None, account_id=ctx.account_id) as conn:
                    row = await (
                        await conn.execute(
                            "SELECT cairn_meta.create_org(%s, %s, %s)",
                            (ctx.account_id, slug, body.name.strip()),
                        )
                    ).fetchone()
                break
            except Exception as exc:  # a taken slug is retried with a suffix
                if "orgs_slug_key" not in str(exc):
                    raise
        else:
            return error(409, "slug_taken")
        assert row is not None  # noqa: S101
        org_id = int(row[0])
        new_ctx = await _ctx_for(rt, ctx.account_id, org_id)
        assert new_ctx is not None  # noqa: S101
        async with scope_of(rt.pool, new_ctx) as conn:
            await record(conn, "org.created", "org", str(org_id), {"slug": slug})
        orgs = await memberships(rt, ctx.account_id)
        response = json_response(201, {"org_id": org_id, "orgs": orgs})
        _cookie(rt, response, ctx.account_id, org_id)
        return response

    @router.post("/session/org")
    async def switch_org(body: SwitchBody, request: Request) -> Response:
        ctx = await resolve_ctx(rt, request)
        if ctx is None or ctx.account_id is None or ctx.demo_persona:
            return error(401, "unauthenticated")
        new_ctx = await _ctx_for(rt, ctx.account_id, body.org_id)
        if new_ctx is None:
            return error(403, "not_a_member")
        async with scope_of(rt.pool, new_ctx) as conn:
            await record(conn, "session.org_switched", "org", str(body.org_id))
        response = json_response(200, {"org_id": body.org_id})
        _cookie(rt, response, ctx.account_id, body.org_id)
        return response

    @router.post("/auth/signout")
    async def signout() -> Response:
        response = json_response(200, {"status": "signed_out"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    app.include_router(router)
