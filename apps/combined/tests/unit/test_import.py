"""Import smoke test: cairn_combined imports from its source tree and finds its dependencies."""

from pathlib import Path

import cairn_api
import cairn_combined
import cairn_worker

MEMBER = Path(__file__).resolve().parents[2]
ROOT = MEMBER.parents[1]


def test_cairn_combined_imports_from_the_workspace_source() -> None:
    assert (
        Path(cairn_combined.__file__).resolve() == MEMBER / "src" / "cairn_combined" / "__init__.py"
    )


def test_sibling_members_resolve_to_the_workspace() -> None:
    assert Path(cairn_api.__file__).resolve() == ROOT / "apps/api/src/cairn_api/__init__.py"
    assert (
        Path(cairn_worker.__file__).resolve() == ROOT / "apps/worker/src/cairn_worker/__init__.py"
    )
