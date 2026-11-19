"""Canary matrix v1 (plan 1.22; PD1-21): every persona on search and answer, with and without
access. Every "with access" cell has a positive control (the canary's own source must be
retrievable), so an empty pipeline cannot pass. HR and partner canaries never reach anyone else.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from cairn_core.testing import FreshDb
from cairn_eval.scenario.access import readers
from cairn_eval.scenario.canary import PLACEHOLDER_RE, canary_token
from cairn_eval.seed import SeededCorpus

from .conftest import login

pytestmark = [pytest.mark.integration, pytest.mark.matrix]

PERSONAS = ("asha", "ben", "carla", "priya")


Case = tuple[str, str, str, set[str], str | None]


def _canary_cases(corpus: SeededCorpus) -> list[Case]:
    """(canary id, string to look for, family, readers, external id of its source or None)."""
    sc = corpus.scenario
    seed = sc.meta.canary_seed
    by_canary: dict[str, str] = {}
    for src in sc.sources:
        texts = [src.title or "", src.body or "", *(m.text for m in src.messages)]
        for text in texts:
            for cid in PLACEHOLDER_RE.findall(text):
                by_canary[cid] = src.id
    cases: list[Case] = []
    for c in sc.canaries:
        text = (c.phrase or "") if c.kind == "phrase" else canary_token(seed, c.id, c.family)
        who: set[str] = set()
        for scope in c.allowed_scopes:
            who |= set(readers(sc, scope, sc.meta.reference_date))
        source_id = by_canary.get(c.id)
        external = corpus.external_id(source_id) if source_id else None
        cases.append((c.id, text, c.family, who, external))
    return cases


def _all_text(body: dict) -> str:
    if "results" in body:
        return " ".join(f"{r['title']} {r['snippet']} {r['citation']}" for r in body["results"])
    cites = " ".join(f"{c['title']} {c['quote']} {c['id']}" for c in body["citations"])
    return f"{body['answer']} {cites}"


def test_matrix_search_and_answer(client: TestClient, seeded: tuple[FreshDb, SeededCorpus]) -> None:
    corpus = seeded[1]
    cases = _canary_cases(corpus)
    assert len(cases) == 8
    cells = positives = 0
    for persona in PERSONAS:
        login(client, persona)
        for cid, needle, _family, who, external in cases:
            can_read = persona in who
            search = client.post("/search", json={"query": needle, "limit": 20}).json()
            answer = client.post("/answer", json={"question": f"What is {needle}?"}).json()
            for name, body in (("search", search), ("answer", answer)):
                text = _all_text(body)
                cells += 1
                if not can_read:
                    assert needle not in text, f"LEAK {cid} to {persona} via {name}"
                    if external:
                        assert f"chunk:{external}@" not in text, (
                            f"LEAK source of {cid} to {persona} via {name}"
                        )
            if can_read and external:
                # positive control: the canary's own source is retrievable on the search path
                assert f"chunk:{external}@" in _all_text(search), (
                    f"{persona} cannot reach {cid}'s source"
                )
                positives += 1
    assert cells == len(PERSONAS) * len(cases) * 2
    assert positives >= 6  # several persona and canary pairs have access


def test_the_expected_access_pattern_holds_for_named_cases(
    client: TestClient, seeded: tuple[FreshDb, SeededCorpus]
) -> None:
    cases = {c[0]: c for c in _canary_cases(seeded[1])}
    # Carla (HR) reads the HR canaries; nobody else among the four does.
    assert {p for p in PERSONAS if p in cases["CAN-HR-01"][3]} == {"carla"}
    # Priya (security) reads the security canaries; Ben's one-week review has been revoked in the oracle date.  # noqa: E501
    assert "priya" in cases["CAN-SEC-01"][3]
    # No persona except HR may ever see the HR token, on any path.
    hr_token = cases["CAN-HR-01"][1]
    for persona in ("asha", "ben", "priya"):
        login(client, persona)
        for path, payload in (
            ("/search", {"query": hr_token}),
            ("/answer", {"question": hr_token}),
        ):
            assert hr_token not in client.post(path, json=payload).text


def test_a_question_with_access_gets_an_answer_and_without_it_the_constant_reply(
    client: TestClient,
) -> None:
    login(client, "carla")
    with_access = client.post("/answer", json={"question": "payroll letters performance"}).json()
    login(client, "ben")
    without = client.post("/answer", json={"question": "payroll letters performance"}).json()
    assert with_access["abstained"] is False
    assert "KP-HR-" in re.sub(
        r"\s", "", with_access["answer"] + " ".join(c["quote"] for c in with_access["citations"])
    )
    # Generic words ("review", "notes") match documents Ben may read, so he can get an answer, but
    # it never carries HR content and never cites an HR source.
    assert "KP-HR-" not in str(without)
    assert not any("hr-private" in c["location"] for c in without["citations"])
