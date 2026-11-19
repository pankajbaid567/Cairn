"""docs/results.md is exactly what the generator produces from the committed Parquet files."""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn_eval import tables

ROOT = Path(__file__).resolve().parents[4]
RUNS = ROOT / "docs" / "evidence" / "phase-5" / "runs"
STUDY = ROOT / "docs" / "spikes" / "S1" / "2026-11-04-results.json"


@pytest.mark.skipif(not RUNS.exists(), reason="no Phase 5 runs recorded yet")
def test_results_md_matches_regeneration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)  # the document names the runs directory as the command line gives it
    expected = tables.build(Path("docs/evidence/phase-5/runs"), STUDY.relative_to(ROOT))
    assert (ROOT / "docs" / "results.md").read_text(encoding="utf-8") == expected, (
        "docs/results.md is stale: run `make eval-tables`"
    )
