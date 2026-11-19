"""Record what C1 v1 is (plan 5.1): the scenario hash, the corpus hash and the question counts.

    python scripts/freeze_c1.py

Writes docs/evidence/phase-5/c1-freeze.json. The numbers are whatever the scenario holds; the plan's
"about 150 questions" is a target, and the file says honestly how many there are.
"""

from __future__ import annotations

import collections
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "eval" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

from cairn_eval.seed import DEFAULT_SCENARIO, load_kestrel  # noqa: E402

OUT = ROOT / "docs" / "evidence" / "phase-5" / "c1-freeze.json"


def main() -> None:
    scenario = load_kestrel(DEFAULT_SCENARIO)
    manifest_path = ROOT / "var" / "corpus" / "kestrel_pay" / "manifest.json"
    corpus_sha = (
        json.loads(manifest_path.read_text())["corpus_sha256"] if manifest_path.exists() else None
    )
    qs = scenario.questions
    record = {
        "corpus": "C1 (Kestrel Pay synthetic company)",
        "scenario_sha256": hashlib.sha256(DEFAULT_SCENARIO.read_bytes()).hexdigest(),
        "corpus_sha256": corpus_sha,
        "questions": len(qs),
        "by_split": dict(sorted(collections.Counter(q.split for q in qs).items())),
        "by_category": dict(sorted(collections.Counter(q.category for q in qs).items())),
        "decisions": len(scenario.decisions),
        "canaries": len(scenario.canaries),
        "target_questions": 150,
        "note": (
            "The plan's target is about 150 questions; the scenario currently holds "
            f"{len(qs)}. Metrics on this set carry wide confidence intervals; results.md "
            "reports them. Growing the set is a content task, not a code one."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(qs)} questions")


if __name__ == "__main__":
    main()
