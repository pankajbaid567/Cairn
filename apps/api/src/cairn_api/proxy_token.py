"""Shared-secret check for requests that must come through the Cloudflare Pages Function.

Design §8 (FT-16). The ``onrender.com`` URL is public, so when ``CAIRN_PROXY_TOKEN`` is set
every path except the liveness probe answers 404 ``not_found`` unless the request carries the
token in ``X-Cairn-Proxy-Token``. A direct hit learns nothing: the answer is the same as for an
unknown route.

The token and the presented header value are never logged and never echoed. This module has no
logger; the access log in ``cairn_api.main`` records the method, the path and the status only.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import SecretStr

EXEMPT_PATHS: Final = frozenset({"/healthz"})
"""Paths served without the token, so Render can probe liveness."""

HEADER: Final = "x-cairn-proxy-token"
"""Header name (HTTP header names are case-insensitive; Starlette lower-cases them)."""


def allowed(path: str, presented: str | None, expected: SecretStr | None) -> bool:
    """True when ``expected`` is None, when ``path`` is exempt, or when ``presented`` equals
    the expected value. The comparison is ``hmac.compare_digest`` over UTF-8 bytes. Pure."""
    if expected is None or path in EXEMPT_PATHS:
        return True
    if presented is None:
        return False
    return hmac.compare_digest(
        presented.encode("utf-8"),
        expected.get_secret_value().encode("utf-8"),
    )


def not_found() -> JSONResponse:
    """The refusal: identical to an unknown route, and never cached."""
    return JSONResponse(
        {"error": "not_found"}, status_code=404, headers={"Cache-Control": "no-store"}
    )


def install(app: FastAPI, expected: SecretStr | None) -> None:
    """Add the check to ``app`` as middleware. Does nothing when no token is configured.

    Call it before the other middleware is added: Starlette puts the last middleware added
    outermost, so the access log then still records the refused requests.
    """
    if expected is None:
        return

    @app.middleware("http")
    async def proxy_token_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not allowed(request.url.path, request.headers.get(HEADER), expected):
            return not_found()
        return await call_next(request)
