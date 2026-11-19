"""Import smoke test: cairn_worker imports from its source tree and finds cairn_core."""

from pathlib import Path

import cairn_core
import cairn_worker

MEMBER = Path(__file__).resolve().parents[2]
CORE_INIT = MEMBER.parents[1] / "packages" / "core" / "src" / "cairn_core" / "__init__.py"


def test_cairn_worker_imports_from_the_workspace_source() -> None:
    assert Path(cairn_worker.__file__).resolve() == MEMBER / "src" / "cairn_worker" / "__init__.py"


def test_cairn_core_resolves_to_the_workspace_member() -> None:
    assert Path(cairn_core.__file__).resolve() == CORE_INIT
