"""The webhook receiver contract shared by the edge Function and the API (plan 3.4, 3.16).

Both receivers do exactly this: verify the HMAC-SHA256 signature in constant time, classify the
event, build a *thin* row (identifiers only, never payload text) and insert it keyed by delivery
id with ``ON CONFLICT DO NOTHING``. A single set of signed fixtures under
``tests/fixtures/webhooks`` is checked against this module and against the TypeScript copy in
``apps/web/functions/_lib/webhook.ts``, so the two cannot drift.

Classes decide drain order: ``acl`` rows (membership, team, collaborator, visibility and
installation events) are applied before ``content`` rows, and every query path waits for them
(drain-before-serve, plan 3.28).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

SIGNATURE_HEADER: Final = "x-hub-signature-256"
EVENT_HEADER: Final = "x-github-event"
DELIVERY_HEADER: Final = "x-github-delivery"
MAX_BODY_BYTES: Final = 1_048_576

ACL_EVENTS: Final = frozenset(
    {
        "membership",  # team membership added or removed
        "team",  # team created, deleted, edited; repository access changed
        "team_add",  # repository added to a team
        "organization",  # member added, removed, invited
        "member",  # outside collaborator added, removed, edited
        "public",  # repository made public
        "repository",  # created, deleted, archived, privatized, publicized, transferred
        "installation",
        "installation_repositories",
    }
)
CONTENT_EVENTS: Final = frozenset(
    {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "issues",
        "issue_comment",
        "push",
    }
)

Class = Literal["acl", "content"]
_REPO: Final = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")
_WORD: Final = re.compile(r"^[a-z_]{1,40}$")
_DELIVERY: Final = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
PRIORITY: Final = {"acl": 0, "content": 1}


@dataclass(frozen=True)
class ThinRow:
    """What the inbox stores: identifiers and a class. Compare with the migration's CHECKs."""

    delivery_id: str
    event: str
    action: str | None
    klass: Class
    installation_id: int | None
    org_id: int | None
    repo_id: int | None
    repo_name: str | None
    object_number: int | None

    @property
    def priority(self) -> int:
        return PRIORITY[self.klass]

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "event": self.event,
            "action": self.action,
            "class": self.klass,
            "installation_id": self.installation_id,
            "org_id": self.org_id,
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "object_number": self.object_number,
        }


def sign(secret: bytes, body: bytes) -> str:
    """The ``X-Hub-Signature-256`` header value for ``body``."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Constant-time check of the header against the HMAC of the raw body. Fails closed."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(header.encode("ascii", "ignore"), sign(secret, body).encode("ascii"))


def classify(event: str) -> Class | None:
    """``acl``, ``content`` or ``None`` for events Cairn does not use (they are acknowledged only)."""  # noqa: E501
    if event in ACL_EVENTS:
        return "acl"
    if event in CONTENT_EVENTS:
        return "content"
    return None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _obj(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, Mapping) else {}


def thin_row(headers: Mapping[str, str], payload: Mapping[str, Any]) -> ThinRow | None:
    """The inbox row for a verified delivery, or ``None`` when the event is not used or malformed.

    ``headers`` must have lower-case names. Only identifiers are copied; a value that does not fit
    the column's pattern becomes ``None`` rather than failing the insert.
    """
    event = headers.get(EVENT_HEADER, "")
    delivery = headers.get(DELIVERY_HEADER, "")
    klass = classify(event)
    if klass is None or not _WORD.match(event) or not _DELIVERY.match(delivery):
        return None
    action = payload.get("action")
    repo = _obj(payload, "repository")
    name = repo.get("full_name")
    number = payload.get("number")
    if number is None:
        number = _obj(payload, "issue").get("number")
    if number is None:
        number = _obj(payload, "pull_request").get("number")
    return ThinRow(
        delivery_id=delivery,
        event=event,
        action=action if isinstance(action, str) and _WORD.match(action) else None,
        klass=klass,
        installation_id=_int(_obj(payload, "installation").get("id")),
        org_id=_int(_obj(payload, "organization").get("id")),
        repo_id=_int(repo.get("id")),
        repo_name=name if isinstance(name, str) and _REPO.match(name) else None,
        object_number=_int(number),
    )


INSERT_SQL: Final = (
    "INSERT INTO cairn.inbox (delivery_id, event, action, class, priority, installation_id, org_id, "  # noqa: E501
    "repo_id, repo_name, object_number) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT DO NOTHING"
)


def insert_params(row: ThinRow) -> tuple[Any, ...]:
    return (
        row.delivery_id, row.event, row.action, row.klass, row.priority, row.installation_id,
        row.org_id, row.repo_id, row.repo_name, row.object_number,
    )  # fmt: skip
