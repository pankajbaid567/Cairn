"""Import smoke test: cairn_api imports from its source tree and finds cairn_core."""

from pathlib import Path

import cairn_api
import cairn_core

MEMBER = Path(__file__).resolve().parents[2]
CORE_INIT = MEMBER.parents[1] / "packages" / "core" / "src" / "cairn_core" / "__init__.py"


def test_cairn_api_imports_from_the_workspace_source() -> None:
    assert Path(cairn_api.__file__).resolve() == MEMBER / "src" / "cairn_api" / "__init__.py"


def test_cairn_core_resolves_to_the_workspace_member() -> None:
    assert Path(cairn_core.__file__).resolve() == CORE_INIT
