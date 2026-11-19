"""Small response helpers shared by the SaaS routers."""

from __future__ import annotations

from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse

NO_STORE = {"Cache-Control": "no-store"}


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={**NO_STORE, **(headers or {})})


def error(status: int, code: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return json_response(status, {"error": code}, headers)


def set_session_cookie(response: Response, name: str, value: str, ttl_s: int, secure: bool) -> None:
    response.set_cookie(
        name, value, max_age=ttl_s, httponly=True, samesite="strict", secure=secure, path="/"
    )


def limited(
    rt: Any, request: Request, bucket: str, limit: int, window_s: int
) -> JSONResponse | None:
    """A 429 when this client has used its allowance for ``bucket`` in the window."""
    limiter = getattr(rt, "auth_limiter", None)
    if limiter is None:
        return None
    client = limiter.client_key(request.headers.get("x-forwarded-for"))
    decision = limiter.check(client, bucket, limit, window_s)
    if decision.allowed:
        return None
    return error(429, "rate_limited", {"Retry-After": str(decision.retry_after_s)})
