"""The verifier (plan 4.2, 4.13; layer L6).

A model's claims are only kept when they check out against what the caller can see:

* every cited id is in the visible, packed evidence set;
* the claim is supported by the spans it cites: enough of its content words, numbers and dates
  appear in the cited text (a claim that adds facts the text does not contain is dropped);
* every date in the claim appears in the cited text, and a date cannot precede the first valid
  time of a decision the claim names (``decision_starts``);
* no canary-format token (``KP-FAMILY-XXXXXXXXXXXXXXXX``) appears in the output unless it also
  appears in the visible evidence. This is the L6 output scan: a token that the caller cannot have
  read must never leave the service, whatever the model did.

Claims that fail are dropped; if none survive the answer abstains with the constant reply. The
report holds reason codes and counts only, never the text of a dropped claim.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Final

from cairn_retrieval.answer import AnswerBody, CitationInfo, ClaimOut, citation_id, not_found
from cairn_retrieval.types import Candidate

CANARY: Final = re.compile(r"\bKP-[A-Z]{2,12}-[A-Z2-7]{16}\b")
ISO_DATE: Final = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DECISION_KEY: Final = re.compile(r"\bADR-(\d{3,4})\b")
_WORD: Final = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")  # "postgresql." -> "postgresql"
_STOP: Final = frozenset(
    [
        "the",
        "a",
        "an",
        "of",
        "to",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "and",
        "or",
        "but",
        "not",
        "for",
        "on",
        "in",
        "at",
        "by",
        "with",
        "from",
        "as",
        "we",
        "they",
        "he",
        "she",
        "them",
        "our",
        "their",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "than",
        "then",
        "so",
        "if",
        "when",
        "which",
        "who",
        "whom",
        "what",
        "why",
        "how",
        "also",
        "into",
        "about",
        "over",
        "after",
        "before",
    ]
)
MIN_SUPPORT: Final = 0.6
MIN_WORDS: Final = 3


@dataclass
class VerifyReport:
    checked: int = 0
    kept: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    canary_hits: int = 0

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def to_json(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "kept": self.kept,
            "dropped": dict(sorted(self.dropped.items())),
            "canary_hits": self.canary_hits,
        }


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if w not in _STOP and len(w) > 1}


def support(claim: str, spans: Sequence[str]) -> float:
    """Fraction of the claim's content words found in the cited spans (1.0 when it has none)."""
    wanted = _words(claim)
    if len(wanted) < MIN_WORDS:
        return 1.0 if wanted <= _words(" ".join(spans)) else 0.0
    have = _words(" ".join(spans))
    return len(wanted & have) / len(wanted)


def dates_in(text: str) -> set[date]:
    out: set[date] = set()
    for y, m, d in ISO_DATE.findall(text):
        try:
            out.add(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    return out


def verify_answer(
    answer: AnswerBody,
    packed: Sequence[Candidate],
    *,
    decision_starts: Mapping[str, date] | None = None,
) -> tuple[AnswerBody, VerifyReport]:
    """Drop what does not check out; abstain if nothing survives. Abstentions pass through."""
    report = VerifyReport()
    if answer.abstained:
        return answer, report
    by_citation = {citation_id(c): c for c in packed}
    visible_text = " ".join(c.text for c in packed)
    visible_canaries = set(CANARY.findall(visible_text))
    kept: list[ClaimOut] = []
    for claim in answer.claims:
        report.checked += 1
        spans = [by_citation[cid].text for cid in claim.citations if cid in by_citation]
        if len(spans) != len(claim.citations) or not spans:
            report.drop("citation_not_visible")
            continue
        found = set(CANARY.findall(claim.text))
        if found - visible_canaries:
            report.canary_hits += 1
            report.drop("canary_scan")
            continue
        if support(claim.text, spans) < MIN_SUPPORT:
            report.drop("unsupported")
            continue
        claim_dates = dates_in(claim.text)
        cited_dates = dates_in(" ".join(spans))
        if claim_dates - cited_dates:
            report.drop("date_not_in_evidence")
            continue
        starts = [
            decision_starts[f"ADR-{n}"]
            for n in DECISION_KEY.findall(claim.text)
            if decision_starts and f"ADR-{n}" in decision_starts
        ]
        if starts and claim_dates and min(claim_dates) < min(starts):
            report.drop("date_before_decision_start")
            continue
        kept.append(claim)
    report.kept = len(kept)
    if len(kept) == len(answer.claims):
        # nothing was dropped, so the free-text answer and the quotes ship as they are: scan them too  # noqa: E501
        quotes = " ".join(c.quote for c in answer.citations)
        leaked = (
            set(CANARY.findall(answer.answer)) | set(CANARY.findall(quotes))
        ) - visible_canaries
        if leaked:
            report.canary_hits += len(leaked)
            report.drop("canary_scan")
            return replace(
                not_found(answer.searched, answer.degraded), verification=report.to_json()
            ), report
    if not kept:
        return replace(
            not_found(answer.searched, answer.degraded), verification=report.to_json()
        ), report
    if len(kept) == len(answer.claims):
        return replace(answer, verification=report.to_json()), report
    # rebuild the answer from the surviving claims, with markers matching the kept citations
    ids: list[str] = []
    for c in kept:
        ids.extend(i for i in c.citations if i not in ids)
    number = {cid: n for n, cid in enumerate(ids, start=1)}
    text = " ".join(
        f"{c.text.rstrip()} " + "".join(f"[{number[i]}]" for i in c.citations) for c in kept
    )
    infos: tuple[CitationInfo, ...] = tuple(i for i in answer.citations if i.id in number)
    infos = tuple(sorted(infos, key=lambda i: number[i.id]))
    return (
        replace(
            answer, answer=text, claims=tuple(kept), citations=infos, verification=report.to_json()
        ),
        report,
    )
