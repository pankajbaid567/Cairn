"""The query registry: every retrieval SQL text lives here (PD1-01, PD1-02).

A template carries one ``{acl:<alias>}`` placeholder per protected relation it reads. ``render``
turns it into ``alias.acl && %(acl_groups)s::int[]`` (L2). Code runs retrieval SQL only through
:func:`run_registered`, so one test (1.20) can prove the predicate is present everywhere, and one
switch (``strip_acl``, test builds only) removes it for the RLS backstop test (1.21).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from cairn_retrieval.access import PrincipalSnapshot

PROTECTED_RELATIONS: Final = frozenset(
    {
        "cairn.items",
        "cairn.revisions",
        "cairn.chunks",
        "cairn.embeddings",
        "cairn.decision_events",
        "cairn.decision_versions",
    }
)
"""Relations with an ``acl`` array column: guarded by ``{acl:alias}``."""

DECISION_RELATIONS: Final = frozenset({"cairn.decisions", "cairn.anchors"})
"""Relations guarded by ``{dvis:alias}``, i.e. ``cairn.decision_visible(alias.decision_id)``."""

CLAIM_RELATIONS: Final = frozenset({"cairn.claims"})
"""Relations guarded by ``{cvis:alias}``, i.e. ``cairn.claim_visible(alias.claim_id)``: a claim is
visible only through one full support set (L5)."""

_PLACEHOLDER: Final = re.compile(r"\{acl:([a-z_][a-z0-9_]*)\}")
_DVIS: Final = re.compile(r"\{dvis:([a-z_][a-z0-9_]*)\}")
_CVIS: Final = re.compile(r"\{cvis:([a-z_][a-z0-9_]*)\}")
STRIP_ENV: Final = "CAIRN_TEST_STRIP_ACL"


class AccessMode(Enum):
    RLS_FIRST = "rls_first"
    DEFINER = "definer"


@dataclass(frozen=True)
class RegisteredQuery:
    name: str
    template: str
    protected_aliases: tuple[str, ...]
    session: tuple[str, ...] = ()  # settings this query depends on, e.g. ("hnsw.ef_search",)
    decision_aliases: tuple[str, ...] = ()  # aliases guarded by {dvis:alias}
    claim_aliases: tuple[str, ...] = ()  # aliases guarded by {cvis:alias}


_REGISTRY: dict[str, RegisteredQuery] = {}


def register(q: RegisteredQuery) -> RegisteredQuery:
    if q.name in _REGISTRY:
        raise ValueError(f"query {q.name!r} is already registered")
    used = tuple(_PLACEHOLDER.findall(q.template))
    if sorted(used) != sorted(q.protected_aliases):
        raise ValueError(f"query {q.name!r}: placeholders {used} differ from protected_aliases")
    if sorted(_DVIS.findall(q.template)) != sorted(q.decision_aliases):
        raise ValueError(f"query {q.name!r}: {{dvis}} placeholders differ from decision_aliases")
    if sorted(_CVIS.findall(q.template)) != sorted(q.claim_aliases):
        raise ValueError(f"query {q.name!r}: {{cvis}} placeholders differ from claim_aliases")
    _REGISTRY[q.name] = q
    return q


def registered() -> Mapping[str, RegisteredQuery]:
    return dict(_REGISTRY)


def strip_allowed() -> bool:
    """Stripping the ACL predicate is possible only in a CI or local test build."""
    return os.environ.get(STRIP_ENV) == "1" and os.environ.get("CAIRN_ENV", "local") in (
        "ci",
        "local",
    )


def render(q: RegisteredQuery, *, strip_acl: bool = False) -> str:
    if strip_acl and not strip_allowed():
        raise RuntimeError(f"strip_acl needs {STRIP_ENV}=1 and CAIRN_ENV ci or local")
    text = q.template
    if strip_acl:
        for pattern in (_PLACEHOLDER, _DVIS, _CVIS):
            text = pattern.sub("TRUE", text)
        return text
    text = _PLACEHOLDER.sub(lambda m: f"{m.group(1)}.acl && %(acl_groups)s::int[]", text)
    text = _DVIS.sub(lambda m: f"cairn.decision_visible({m.group(1)}.decision_id)", text)
    return _CVIS.sub(lambda m: f"cairn.claim_visible({m.group(1)}.claim_id)", text)


class PrincipalMismatch(RuntimeError):
    """The connection's principal setting is not the snapshot's: refuse to run."""


async def run_registered(
    conn: AsyncConnection[Any],
    name: str,
    params: Mapping[str, object],
    snapshot: PrincipalSnapshot,
    *,
    mode: AccessMode = AccessMode.RLS_FIRST,
    strip_acl: bool = False,
) -> list[dict[str, Any]]:
    """Run a registered query for ``snapshot``. The only way retrieval SQL is executed."""
    if mode is not AccessMode.RLS_FIRST:
        raise NotImplementedError("definer mode is not built; ADR-0006 chose rls_first")
    query = _REGISTRY[name]
    async with conn.cursor(row_factory=dict_row) as check:
        await check.execute("SELECT current_setting('cairn.principal_groups', true) AS g")
        row = await check.fetchone()
    if row is None or row["g"] != snapshot.setting():
        raise PrincipalMismatch("principal setting differs from the snapshot")
    bound = {**params, "acl_groups": list(snapshot.groups)}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(render(query, strip_acl=strip_acl).encode(), bound)
        return await cur.fetchall()
