"""Question files for the eval harness (design §18; Property 25).

One JSON record per line, ``format: cairn-questions-v1``. ``gold_in_scope`` keeps the gold sources
the importers actually load (manifest status ``rendered``), because Phase 1 imports no pull
requests or issues (PD1-20).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from cairn_eval.scenario.model import Scenario

FORMAT: Final = "cairn-questions-v1"


class QuestionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["cairn-questions-v1"] = FORMAT
    qid: str = Field(pattern=r"^Q\d{3}$")
    corpus: str = "C1"
    split: Literal["dev", "test"]
    category: str
    asker: str
    text: str
    as_of: dict[str, str] | None = None
    gold_evidence: list[str] = []
    gold_in_scope: list[str] = []
    expect_abstain: bool = False
    must_not_contain: list[str] = []

    @property
    def person(self) -> str:
        """``agent:asha`` acts for ``asha``."""
        return self.asker.removeprefix("agent:")

    @property
    def eligible(self) -> bool:
        return bool(self.gold_in_scope) and not self.expect_abstain


def export_questions(
    scenario: Scenario, manifest: dict[str, Any], split: str
) -> list[QuestionRecord]:
    rendered = {sid for sid, info in manifest["sources"].items() if info["status"] == "rendered"}
    out = []
    for q in scenario.questions:
        if q.split != split:
            continue
        out.append(
            QuestionRecord(
                qid=q.id,
                split=q.split,
                category=q.category,
                asker=q.asker,
                text=q.text,
                as_of=(
                    {"valid": q.as_of.valid.isoformat(), "recorded": q.as_of.recorded.isoformat()}
                    if q.as_of
                    else None
                ),
                gold_evidence=list(q.expected.evidence),
                gold_in_scope=[e for e in q.expected.evidence if e in rendered],
                expect_abstain=q.expected.abstain,
                must_not_contain=list(q.expected.must_not_contain),
            )
        )
    return sorted(out, key=lambda r: r.qid)


def write_questions(path: Path, records: Iterable[QuestionRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r.model_dump(), sort_keys=True, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_questions(path: Path) -> list[QuestionRecord]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                out.append(QuestionRecord.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"{path.name}:{n}: not a valid question record") from exc
    return out


def json_schema() -> dict[str, Any]:
    return QuestionRecord.model_json_schema()
