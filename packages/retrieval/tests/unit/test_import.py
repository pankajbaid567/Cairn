"""Import smoke test: cairn_retrieval imports from its source tree and finds cairn_core."""

from pathlib import Path

import cairn_core
import cairn_retrieval

MEMBER = Path(__file__).resolve().parents[2]
CORE_INIT = MEMBER.parents[1] / "packages" / "core" / "src" / "cairn_core" / "__init__.py"


def test_cairn_retrieval_imports_from_the_workspace_source() -> None:
    expected = MEMBER / "src" / "cairn_retrieval" / "__init__.py"
    assert Path(cairn_retrieval.__file__).resolve() == expected


def test_cairn_core_resolves_to_the_workspace_member() -> None:
    assert Path(cairn_core.__file__).resolve() == CORE_INIT
