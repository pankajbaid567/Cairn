"""Database helpers for runtime processes (design §3; PD1-03).

Every runtime connection (API pool, importer, worker, eval) is created here so they share the
same rules:

- ``prepare_threshold=None``: psycopg's auto-prepare must not reuse a plan across principals
  (S2 hazard), so statements are never prepared.
- TLS keywords from ``libpq_tls`` reach every connection (FT-14).
- Per-request session settings go through ``set_local``, which accepts only an allowlist of
  names, so no request value can ever name a setting.
"""

from __future__ import annotations

from typing import Any, Final

import psycopg
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from cairn_core.config import CommonSettings, libpq_tls

RUNTIME_CONNECT_KWARGS: Final[dict[str, Any]] = {"prepare_threshold": None, "autocommit": False}

ALLOWED_SETTINGS: Final = frozenset(
    {
        "cairn.org_id",
        "cairn.account_id",
        "cairn.principal_id",
        "cairn.principal_groups",
        "cairn.principal_set_hash",
        "hnsw.ef_search",
        "hnsw.iterative_scan",
        "hnsw.max_scan_tuples",
    }
)
"""The only settings ``set_local`` may write."""


def conninfo(
    settings: CommonSettings, user: str, password: str, *, application_name: str, dbname: str | None
) -> str:
    """A libpq connection string for ``user``; the password is inside it, so never log it."""
    options = f"-c cairn.org_id={settings.org_id}" if settings.org_id is not None else None
    return make_conninfo(
        **({"options": options} if options else {}),
        host=settings.db_host,
        port=settings.db_port,
        dbname=dbname or settings.db_name,
        user=user,
        password=password,
        connect_timeout=max(1, int(settings.db_timeout_s)),
        application_name=application_name,
        **libpq_tls(settings),
    )


def connect(
    settings: CommonSettings,
    user: str,
    password: str,
    *,
    application_name: str,
    dbname: str | None = None,
    autocommit: bool = False,
) -> psycopg.Connection[Any]:
    """Open a synchronous runtime connection (importer, eval, tests)."""
    kwargs = {**RUNTIME_CONNECT_KWARGS, "autocommit": autocommit}
    return psycopg.connect(
        conninfo(settings, user, password, application_name=application_name, dbname=dbname),
        **kwargs,
    )


def make_async_pool(
    settings: CommonSettings,
    user: str,
    password: str,
    *,
    application_name: str,
    max_size: int = 4,
    min_size: int = 1,
    dbname: str | None = None,
) -> AsyncConnectionPool[AsyncConnection[Any]]:
    """An async pool for the API (``cairn_app``), created closed: open it in the app lifespan.

    Small on purpose: Neon free has few connections and the combined process has 512 MB.
    """
    return AsyncConnectionPool(
        conninfo(settings, user, password, application_name=application_name, dbname=dbname),
        min_size=min_size,
        max_size=max_size,
        kwargs=dict(RUNTIME_CONNECT_KWARGS),
        check=AsyncConnectionPool.check_connection,
        open=False,
        name=application_name,
    )


def _check_setting(name: str) -> str:
    if name not in ALLOWED_SETTINGS:
        raise ValueError(f"setting {name!r} is not allowed")
    return name


def set_local(conn: psycopg.Connection[Any], name: str, value: str) -> None:
    """``set_config(name, value, true)``: lasts until the end of the current transaction."""
    conn.execute("SELECT set_config(%s, %s, true)", (_check_setting(name), value))


async def aset_local(conn: AsyncConnection[Any], name: str, value: str) -> None:
    """Async twin of :func:`set_local`."""
    await conn.execute("SELECT set_config(%s, %s, true)", (_check_setting(name), value))
