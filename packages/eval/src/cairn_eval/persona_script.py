"""The Phase 1 persona script (exit gate; design §19). One implementation, three callers:
the in-process test, ``scripts/persona_script.py`` against a running stack, and
``scripts/deploy_smoke.py`` against the demo deployment.

The check is written against the HTTP API only, with a callable ``ask(path, payload)`` per persona.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from cairn_eval.scenario.access import readers
from cairn_eval.scenario.canary import canary_token
from cairn_eval.scenario.model import Scenario

PERSONAS: Final = ("asha", "ben", "carla", "priya")
SCRIPT: Final = (
    Path(__file__).resolve().parents[4] / "scenarios" / "kestrel_pay" / "phase1-persona-script.yaml"
)

Ask = Callable[[str, str, dict[str, Any]], dict[str, Any]]
"""``ask(persona, path, payload) -> JSON body``"""


@dataclass
class Finding:
    persona: str
    question: str
    problem: str


def load_script(path: Path = SCRIPT) -> list[dict[str, Any]]:
    return list(yaml.safe_load(path.read_text(encoding="utf-8"))["questions"])


def can_read(scenario: Scenario, manifest: dict[str, Any], persona: str, source_id: str) -> bool:
    info = manifest["sources"][source_id]
    scope = info.get("scope")
    return bool(scope) and persona in readers(scenario, scope, scenario.meta.reference_date)


def run_script(
    scenario: Scenario, manifest: dict[str, Any], ask: Ask, *, personas: tuple[str, ...] = PERSONAS
) -> list[Finding]:
    """Findings (empty when the gate passes)."""
    findings: list[Finding] = []
    canaries = []  # (string, people who may see it: readers of any allowed scope)
    for c in scenario.canaries:
        who: set[str] = set()
        for scope in c.allowed_scopes:
            who |= set(readers(scenario, scope, scenario.meta.reference_date))
        text = (
            c.phrase
            if c.kind == "phrase"
            else canary_token(scenario.meta.canary_seed, c.id, c.family)
        )
        canaries.append((text or "", who))
    ext = {sid: i["external_id"] for sid, i in manifest["sources"].items() if i.get("external_id")}
    changed: dict[str, set[tuple[str, ...]]] = {}
    for q in load_script():
        gold = q["gold"]
        for persona in personas:
            search = ask(persona, "/search", {"query": q["text"], "limit": 5})
            answer = ask(persona, "/answer", {"question": q["text"]})
            top5 = " ".join(r["citation"] for r in search.get("results", [])[:5])
            cited = " ".join(c["id"] for c in answer.get("citations", []))
            text = str(search) + str(answer)
            changed.setdefault(q["id"], set()).add(
                tuple(r["citation"] for r in search.get("results", [])[:5])
            )
            readable = [g for g in gold if can_read(scenario, manifest, persona, g)]
            hidden = [g for g in gold if not can_read(scenario, manifest, persona, g)]
            if readable and not any(f"chunk:{ext[g]}@" in top5 for g in readable):
                findings.append(
                    Finding(persona, q["id"], "a readable gold source is not in the top 5")
                )
            if (
                readable
                and not any(f"chunk:{ext[g]}@" in cited for g in readable)
                and not answer.get("abstained")
            ):
                findings.append(Finding(persona, q["id"], "answer cites no readable gold source"))
            for g in hidden:
                if f"chunk:{ext[g]}@" in top5 + cited:
                    findings.append(
                        Finding(persona, q["id"], f"restricted source {g} reached the output")
                    )
            for tok, who in canaries:
                if tok and tok in text and persona not in who:
                    findings.append(
                        Finding(persona, q["id"], "a canary appeared for a persona with no access")
                    )
    for qid, variants in changed.items():
        if len(variants) < 2:
            findings.append(Finding("*", qid, "switching persona did not change the results"))
    return findings
