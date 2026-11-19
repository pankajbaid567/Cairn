"""Map MADR, KEP and PEP status fields onto the decision lifecycle (plan 2.7; D3).

Lifecycle states: proposed, accepted, rejected, withdrawn, implemented, superseded, deprecated,
reverted. Standards differ: PEP "Final" is accepted and done; KEP "implementable" is accepted and
"replaced" is superseded. A decision with no single approver (consensus, "rough consensus") maps
like any other accepted decision and is flagged ``no_single_approver`` (open question 7), so the
review UI can ask for an owner instead of inventing one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

Format = Literal["madr", "kep", "pep"]

_TABLE: Final[dict[Format, dict[str, str]]] = {
    "madr": {
        "proposed": "proposed",
        "accepted": "accepted",
        "rejected": "rejected",
        "deprecated": "deprecated",
        "superseded": "superseded",
    },
    "kep": {
        "provisional": "proposed",
        "implementable": "accepted",
        "implemented": "implemented",
        "deferred": "proposed",
        "rejected": "rejected",
        "withdrawn": "withdrawn",
        "replaced": "superseded",
    },
    "pep": {
        "draft": "proposed",
        "accepted": "accepted",
        "final": "implemented",
        "active": "accepted",
        "rejected": "rejected",
        "withdrawn": "withdrawn",
        "deferred": "proposed",
        "superseded": "superseded",
        "provisional": "proposed",
    },
}
_BY: Final = re.compile(r"(?:superseded|replaced)\s+by\s+([A-Za-z]+[- ]?\d+)", re.IGNORECASE)
_CONSENSUS: Final = re.compile(r"consensus|no single (?:owner|approver)|community", re.IGNORECASE)


@dataclass(frozen=True)
class StatusMapping:
    state: str | None  # None: the text is not a status this standard knows
    superseded_by: str | None = None
    no_single_approver: bool = False


def map_status(fmt: Format, raw: str) -> StatusMapping:
    """``StatusMapping`` for a status string such as ``"Accepted"`` or ``"superseded by ADR-0012"``."""  # noqa: E501
    text = raw.strip()
    by = _BY.search(text)
    head = re.split(r"[\s(:,;]", text.lower(), maxsplit=1)[0] if text else ""
    state = _TABLE[fmt].get(head)
    if by and state is None:
        state = "superseded"
    return StatusMapping(
        state,
        by.group(1).replace(" ", "-").upper() if by else None,
        bool(_CONSENSUS.search(text)),
    )
