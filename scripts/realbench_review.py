"""Compare the AI judge with a person's labels.

Open docs/evidence/real-bench/judge_review_sheet.csv in a spreadsheet. For each row read the
question, the reference answer, the system answer and the quote, then fill:
  human_correct   yes | partial | no     (does the system answer agree with the reference answer?)
  human_grounded  yes | no               (is every claim supported by the quote shown?)
Save the file, then:

    uv run python scripts/realbench_review.py

It prints agreement and Cohen's kappa for both labels, and the judge's headline numbers recomputed
on the person's labels. Rows left empty are ignored, so a partial review still works.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

SHEET = Path("docs/evidence/real-bench/judge_review_sheet.csv")


def kappa(a: list[str], b: list[str]) -> float:
    n = len(a)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def main() -> int:
    rows = [r for r in csv.DictReader(SHEET.open()) if r["human_correct"].strip() and r["human_grounded"].strip()]
    if not rows:
        print("no human labels filled in yet")
        return 1
    for field in ("correct", "grounded"):
        ai = [r[f"ai_{field}"] for r in rows]
        hu = [r[f"human_{field}"].strip().lower() for r in rows]
        agree = sum(x == y for x, y in zip(ai, hu, strict=True))
        print(f"{field}: {agree} of {len(rows)} agree ({agree / len(rows):.0%}), kappa {kappa(ai, hu):.2f}")
        print("  AI says yes:", sum(x == "yes" for x in ai) / len(ai), " human says yes:", sum(x == "yes" for x in hu) / len(hu))
    return 0


if __name__ == "__main__":
    sys.exit(main())
