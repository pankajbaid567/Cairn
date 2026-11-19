"""The combined process spends its 512 MB on serving code only (design §1, ADR-0014): importing it
must not pull in the evaluation package, pyarrow, numpy or the test-only seeding code."""

from __future__ import annotations

import subprocess
import sys

FORBIDDEN = ("cairn_eval", "pyarrow", "numpy", "pytest", "hypothesis")


def test_importing_the_combined_app_loads_no_evaluation_code() -> None:
    code = (
        "import sys, cairn_combined.app, cairn_api.main, cairn_worker.drainer_setup;"
        f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"combined imports forbidden modules: {result.stdout.strip()}"
