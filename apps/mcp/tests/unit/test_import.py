"""Import smoke test: cairn_mcp imports from its source tree and finds cairn_core."""

from pathlib import Path

import cairn_core
import cairn_mcp

MEMBER = Path(__file__).resolve().parents[2]
CORE_INIT = MEMBER.parents[1] / "packages" / "core" / "src" / "cairn_core" / "__init__.py"


def test_cairn_mcp_imports_from_the_workspace_source() -> None:
    assert Path(cairn_mcp.__file__).resolve() == MEMBER / "src" / "cairn_mcp" / "__init__.py"


def test_cairn_core_resolves_to_the_workspace_member() -> None:
    assert Path(cairn_core.__file__).resolve() == CORE_INIT
