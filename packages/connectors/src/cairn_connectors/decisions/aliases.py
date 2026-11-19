"""Alias resolution (plan 2.8): deterministic matching first, then embeddings with LLM confirmation.

Every merge is reviewable: an alias created by the embedding path is ``confirmed = false`` and has an
``alias`` review item until a human or the confirming model accepts it. Deterministic matches
(exact, then normalized) are confirmed at once.
"""  # noqa: E501

from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import psycopg

Conn = psycopg.Connection[Any]
Confirm = Callable[[str, str], Awaitable[bool]]
"""``confirm(candidate, existing) -> same entity?`` backed by the LLM through the neuron budget."""

_PUNCT = re.compile(r"[^\w\s]")


def normalize_name(name: str) -> str:
    folded = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(_PUNCT.sub(" ", folded).split())


def resolve(conn: Conn, kind: str, name: str) -> int | None:
    """The entity id for ``name`` by exact then normalized match, or ``None``."""
    row = conn.execute(
        "SELECT e.entity_id FROM cairn.entities e WHERE e.kind = %s AND e.canonical = %s "
        "UNION SELECT a.entity_id FROM cairn.aliases a JOIN cairn.entities e USING (entity_id) "
        "WHERE e.kind = %s AND a.alias = %s LIMIT 1",
        (kind, name, kind, name),
    ).fetchone()
    if row:
        return int(row[0])
    norm = normalize_name(name)
    for entity_id, canonical in conn.execute(
        "SELECT entity_id, canonical FROM cairn.entities WHERE kind = %s", (kind,)
    ).fetchall():
        if normalize_name(str(canonical)) == norm:
            conn.execute(
                "INSERT INTO cairn.aliases (entity_id, alias, method, confirmed) VALUES (%s, %s, 'normalized', true) "  # noqa: E501
                "ON CONFLICT DO NOTHING",
                (entity_id, name),
            )
            return int(entity_id)
    for entity_id, alias in conn.execute(
        "SELECT a.entity_id, a.alias FROM cairn.aliases a JOIN cairn.entities e USING (entity_id) WHERE e.kind = %s",  # noqa: E501
        (kind,),
    ).fetchall():
        if normalize_name(str(alias)) == norm:
            return int(entity_id)
    return None


def ensure_entity(conn: Conn, kind: str, name: str) -> int:
    found = resolve(conn, kind, name)
    if found is not None:
        return found
    row = conn.execute(
        "INSERT INTO cairn.entities (kind, canonical) VALUES (%s, %s) RETURNING entity_id",
        (kind, name),
    ).fetchone()
    assert row is not None  # noqa: S101
    return int(row[0])


async def resolve_fuzzy(
    conn: Conn,
    kind: str,
    name: str,
    candidates: Sequence[tuple[int, str, float]],
    confirm: Confirm | None,
    *,
    threshold: float = 0.85,
) -> int | None:
    """Embedding path: ``candidates`` are ``(entity_id, canonical, similarity)`` from a vector search.

    The best candidate above ``threshold`` becomes an alias. With a ``confirm`` function (the LLM) the
    alias is confirmed when it agrees; without one, or when it disagrees or is unsure, the alias is
    left unconfirmed and a review item is opened. Nothing merges silently.
    """  # noqa: E501
    best = max((c for c in candidates if c[2] >= threshold), key=lambda c: c[2], default=None)
    if best is None:
        return None
    entity_id, canonical, similarity = best
    agreed = bool(confirm) and await confirm(name, canonical)  # type: ignore[misc]
    conn.execute(
        "INSERT INTO cairn.aliases (entity_id, alias, method, confirmed) VALUES (%s, %s, 'embedding', %s) "  # noqa: E501
        "ON CONFLICT (entity_id, alias) DO UPDATE SET confirmed = aliases.confirmed OR EXCLUDED.confirmed",  # noqa: E501
        (entity_id, name, agreed),
    )
    if not agreed:
        conn.execute(
            "INSERT INTO cairn.review_items (kind, detail) VALUES ('alias', %s::jsonb)",
            (
                '{"alias": %s, "canonical": %s, "similarity": %s}'  # noqa: UP031
                % (_j(name), _j(canonical), round(similarity, 4)),
            ),
        )
    return entity_id if agreed else None


def _j(s: str) -> str:
    import json

    return json.dumps(s)
