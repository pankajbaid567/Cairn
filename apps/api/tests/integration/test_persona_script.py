"""The Phase 1 exit-gate persona script, in process with the fakes (design §19)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn_core.testing import FreshDb
from cairn_eval.persona_script import PERSONAS, load_script, run_script
from cairn_eval.seed import SeededCorpus

from .conftest import login

pytestmark = [pytest.mark.integration, pytest.mark.matrix]


def test_the_script_has_five_questions_with_gold_sources_in_the_manifest(
    seeded: tuple[FreshDb, SeededCorpus],
) -> None:
    questions = load_script()
    assert len(questions) == 5
    for q in questions:
        for g in q["gold"]:
            assert seeded[1].manifest["sources"][g]["status"] == "rendered"


def test_switching_persona_changes_results_and_nothing_restricted_leaks(
    client: TestClient, seeded: tuple[FreshDb, SeededCorpus]
) -> None:
    def ask(persona: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        login(client, persona)
        return client.post(path, json=payload).json()

    findings = run_script(seeded[1].scenario, seeded[1].manifest, ask, personas=PERSONAS)
    assert findings == [], [f.__dict__ for f in findings]
