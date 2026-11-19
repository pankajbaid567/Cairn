"""Audit log v1: ids and hashes only (PD1-16, Property 22).

The question is stored as an HMAC-SHA-256 under ``CAIRN_AUDIT_HASH_KEY`` (a plain hash of a short
question is easy to reverse), the answer as the SHA-256 of its canonical JSON, and the result as
item ids. The API inserts without ``RETURNING`` because it has no SELECT policy on the table.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from cairn_core.db import aset_local
from cairn_core.tenancy import current_org
from cairn_retrieval.access import PrincipalSnapshot

Endpoint = Literal["search", "answer", "decision"]
Outcome = Literal["results", "no_results", "answered", "not_found"]


@dataclass(frozen=True)
class AuditRecord:
    audit_id: UUID
    endpoint: Endpoint
    principal_id: int
    acting_for: int | None
    principal_groups: tuple[int, ...]
    acl_version: int
    question_hmac: str
    returned_item_ids: tuple[int, ...]
    answer_sha256: str | None
    outcome: Outcome
    model: str | None


def normalize_question(question: str) -> str:
    return " ".join(question.split()).casefold()


def question_hmac(question: str, key: bytes) -> str:
    return hmac.new(key, normalize_question(question).encode("utf-8"), hashlib.sha256).hexdigest()


def canonical_json(body: object) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_audit_record(
    snapshot: PrincipalSnapshot,
    endpoint: Endpoint,
    question: str,
    response_body: object,
    *,
    key: bytes,
    returned_item_ids: Sequence[int],
    outcome: Outcome,
    model: str | None = None,
) -> AuditRecord:
    answer_sha = (
        hashlib.sha256(canonical_json(response_body).encode("utf-8")).hexdigest()
        if endpoint == "answer"
        else None
    )
    return AuditRecord(
        audit_id=uuid4(),
        endpoint=endpoint,
        principal_id=snapshot.principal_id,
        acting_for=snapshot.acting_for,
        principal_groups=snapshot.groups,
        acl_version=snapshot.acl_version,
        question_hmac=question_hmac(question, key),
        returned_item_ids=tuple(sorted(set(returned_item_ids))),
        answer_sha256=answer_sha,
        outcome=outcome,
        model=model,
    )


async def write_audit(pool: AsyncConnectionPool[AsyncConnection[Any]], rec: AuditRecord) -> None:
    """One short transaction. Raises on failure: no answer without an audit row."""
    effective = rec.acting_for if rec.acting_for is not None else rec.principal_id
    async with pool.connection() as conn, conn.transaction():
        org = current_org()
        if org is not None:
            await aset_local(conn, "cairn.org_id", str(org))
        await aset_local(conn, "cairn.principal_id", str(effective))
        await conn.execute(
            "INSERT INTO cairn.audit_log (audit_id, endpoint, principal_id, acting_for, "
            "principal_groups, acl_version, question_hmac, returned_item_ids, answer_sha256, "
            "outcome, model) VALUES (%s, %s, %s, %s, %s::int[], %s, %s, %s::bigint[], %s, %s, %s)",
            (
                rec.audit_id,
                rec.endpoint,
                rec.principal_id,
                rec.acting_for,
                list(rec.principal_groups),
                rec.acl_version,
                rec.question_hmac,
                list(rec.returned_item_ids),
                rec.answer_sha256,
                rec.outcome,
                rec.model,
            ),
        )
