"""Import smoke test: cairn_core imports from this workspace member's source tree."""

from pathlib import Path

import cairn_core

MEMBER = Path(__file__).resolve().parents[2]


def test_cairn_core_imports_from_the_workspace_source() -> None:
    assert Path(cairn_core.__file__).resolve() == MEMBER / "src" / "cairn_core" / "__init__.py"
