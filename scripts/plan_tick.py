#!/usr/bin/env python3
"""Tick items in development_plan.md and add a progress-log row.

usage: plan_tick.py --tick 1.3 1.4 ... [--log "date|phase|done|blocked|next"]

Only the checkbox of the named item changes (``- [ ] N.M`` becomes ``- [x] N.M``). The plan is a
source document: after any edit regenerate the hash record
(``shasum -a 256 brainstorm.md development_plan.md > docs/evidence/phase-0/source-docs.sha256``).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parents[1] / "development_plan.md"


def tick(text: str, ids: list[str]) -> tuple[str, list[str]]:
    missing = []
    for item in ids:
        pattern = re.compile(rf"^(\s*- )\[ \]( {re.escape(item)} )", re.MULTILINE)
        text, n = pattern.subn(r"\1[x]\2", text)
        if n == 0 and not re.search(rf"^\s*- \[x\] {re.escape(item)} ", text, re.MULTILINE):
            missing.append(item)
    return text, missing


def refresh_hashes() -> None:
    """The plan is a source document: keep docs/evidence/phase-0/source-docs.sha256 in step."""
    import hashlib

    root = PLAN.parent
    lines = [
        f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}"
        for name in ("brainstorm.md", "development_plan.md")
    ]
    (root / "docs/evidence/phase-0/source-docs.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", nargs="*", default=[])
    ap.add_argument("--log", action="append", default=[])
    args = ap.parse_args()
    text = PLAN.read_text(encoding="utf-8")
    text, missing = tick(text, args.tick)
    for row in args.log:
        cells = [c.strip() for c in row.split("|")]
        if len(cells) != 5:
            print("--log needs 5 cells separated by |", file=sys.stderr)
            return 2
        text = text.rstrip("\n") + "\n| " + " | ".join(cells) + " |\n"
    PLAN.write_text(text, encoding="utf-8")
    refresh_hashes()
    if missing:
        print("not found:", ", ".join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
