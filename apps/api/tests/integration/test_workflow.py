"""The seven-step workflow end to end (plan 4.1, 4.13)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus
from cairn_retrieval.types import RetrievalConfig
from cairn_retrieval.workflow import STEPS, analyze, plan

from .conftest import login, make_app_client

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def wclient(seeded: tuple[FreshDb, SeededCorpus]) -> Iterator[TestClient]:
    yield from make_app_client(seeded[0], CAIRN_ANSWER_CACHE_SIZE=0)


def test_an_answer_reports_the_seven_steps_in_order_with_timings(wclient: TestClient) -> None:
    login(wclient, "asha")
    body = wclient.post(
        "/answer", json={"question": "Why did we move payment-service to PostgreSQL?"}
    ).json()
    assert body["abstained"] is False
    assert [t["step"] for t in body["trace"]] == list(STEPS)
    assert all(isinstance(t["ms"], float) and t["ms"] >= 0 for t in body["trace"])
    assert body["verification"]["kept"] >= 1
    assert body["verification"]["dropped"] == {}


def test_every_step_opens_a_span(wclient: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib

    import cairn_api.search as search_module

    names: list[str] = []

    def spy(name: str) -> Any:
        names.append(name)
        return contextlib.nullcontext()

    monkeypatch.setattr(search_module, "_step_span", spy)
    login(wclient, "asha")
    wclient.post("/answer", json={"question": "Why did we move payment-service to PostgreSQL?"})
    assert names == list(STEPS)


def test_the_not_found_reply_carries_no_trace_or_verification(wclient: TestClient) -> None:
    login(wclient, "asha")
    body = wclient.post("/answer", json={"question": "zzqx plugh frobnicate xyzzy"}).json()
    assert body["abstained"] is True
    assert "trace" not in body
    assert "verification" not in body


def test_a_model_that_invents_claims_is_verified_down_to_an_abstention(
    wclient: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that returns well-formed JSON with invented claims cannot get them out."""
    import json

    from cairn_retrieval.llm.types import LlmRequest, LlmResponse

    class Liar:
        provider, model = "fake", "liar"

        async def complete(self, req: LlmRequest) -> LlmResponse:
            claims = [
                {
                    "text": "Kafka replaced PostgreSQL in 2031 across every region",
                    "evidence": ["E1"],
                }
            ]
            return LlmResponse(
                text=json.dumps({"abstain": False, "answer": "x [E1]", "claims": claims}),
                tokens_in=1,
                tokens_out=1,
                latency_ms=0.0,
                provider="fake",
                model="liar",
            )

    import cairn_api.search as search_module

    original = search_module.answer_workflow

    async def with_liar(*args: Any, **kwargs: Any) -> Any:
        args = (args[0], args[1], args[2], Liar(), *args[4:])
        return await original(*args, **kwargs)

    monkeypatch.setattr(search_module, "answer_workflow", with_liar)
    login(wclient, "asha")
    body = wclient.post(
        "/answer", json={"question": "Why did we move payment-service to PostgreSQL?"}
    ).json()
    assert body["abstained"] is True
    assert body["citations"] == []


def test_analyze_and_plan_are_pure_and_name_the_decisions_in_the_question() -> None:
    a = analyze("What changed between ADR-012 and ADR-018 on 2026-07-15? ADR-012 again")
    assert a.decision_keys == ("ADR-012", "ADR-018")
    assert a.dates == ("2026-07-15",)
    assert plan(a, RetrievalConfig()).expand_decisions is True
    assert plan(analyze("plain question"), RetrievalConfig()).expand_decisions is False


def test_answer_latency_p95_over_the_scripted_questions_is_recorded(wclient: TestClient) -> None:
    """Plan 4.20: p95 of the workflow (all seven steps) locally. Models are the deterministic
    fakes, so this measures Cairn's own pipeline, not Workers AI; the deployment is reported
    separately (warm at 0.1 CPU, and the cold-start wake)."""
    import json
    import time
    from pathlib import Path

    from cairn_eval.metrics import percentile
    from cairn_eval.persona_script import PERSONAS, load_script

    steps: dict[str, list[float]] = {s: [] for s in STEPS}
    totals: list[float] = []
    for persona in PERSONAS:
        login(wclient, persona)
        for q in load_script():
            t0 = time.perf_counter()
            r = wclient.post("/answer", json={"question": q["text"]})
            totals.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200
            for t in r.json().get("trace", []):
                steps[t["step"]].append(t["ms"])
    summary = {
        "n": len(totals),
        "p50_ms": round(percentile(totals, 50), 1),
        "p95_ms": round(percentile(totals, 95), 1),
        "max_ms": round(max(totals), 1),
        "per_step_p95_ms": {s: round(percentile(v, 95), 1) for s, v in steps.items() if v},
        "note": "fake models, local compose; excludes Workers AI latency and Render cold start",
    }
    out = Path(__file__).resolve().parents[4] / "docs" / "evidence" / "phase-4"
    out.mkdir(parents=True, exist_ok=True)
    (out / "answer-latency-2026-11-04.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    assert len(totals) >= 20
    assert percentile(totals, 95) <= 5000, summary  # the Proposed target
