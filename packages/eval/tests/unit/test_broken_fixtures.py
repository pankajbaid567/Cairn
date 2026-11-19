"""Broken-fixture tests (task 12.7; requirements 13.26 and 13.27).

Each ``*.patch.yaml`` under ``scenarios/kestrel_pay/fixtures/broken`` is an RFC 6902
patch applied to the loaded ``scenario.yaml``. The validator must report every rule in
``expect_rules`` and nothing outside ``expect_rules`` plus ``allow_rules``.
"""

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonpatch
import pytest
import yaml

from cairn_eval.scenario.cli import main as cli_main
from cairn_eval.scenario.loader import load_scenario
from cairn_eval.scenario.validator import validate

REPO_ROOT = Path(__file__).resolve().parents[4]
SCENARIO_FILE = REPO_ROOT / "scenarios" / "kestrel_pay" / "scenario.yaml"
FIXTURE_DIR = REPO_ROOT / "scenarios" / "kestrel_pay" / "fixtures" / "broken"
FIXTURES = sorted(FIXTURE_DIR.glob("*.patch.yaml"))
EXPECTED_NAMES = {
    "inconsistent-date.patch.yaml",
    "twist-without-questions.patch.yaml",
    "unplaced-canary.patch.yaml",
}


def _load_fixture(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _scenario() -> Any:
    return copy.deepcopy(load_scenario(SCENARIO_FILE))


def _patched(fixture: Mapping[str, Any]) -> Any:
    return jsonpatch.apply_patch(_scenario(), fixture["patch"])


def test_exactly_three_fixtures() -> None:
    assert {p.name for p in FIXTURES} == EXPECTED_NAMES


def test_unpatched_scenario_passes() -> None:
    assert validate(_scenario()) == []


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name.removesuffix(".patch.yaml"))
class TestFixture:
    def test_fixture_shape(self, path: Path) -> None:
        fixture = _load_fixture(path)
        assert set(fixture) == {"description", "expect_rules", "allow_rules", "patch"}
        assert fixture["description"]
        assert fixture["expect_rules"]
        assert fixture["patch"][0]["op"] == "test"

    def test_reports_expected_rules_only(self, path: Path) -> None:
        fixture = _load_fixture(path)
        reported = {v.rule for v in validate(_patched(fixture))}
        expected = set(fixture["expect_rules"])
        allowed = expected | set(fixture["allow_rules"])
        assert expected <= reported
        assert reported <= allowed

    def test_cli_exits_1(
        self, path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fixture = _load_fixture(path)
        target = tmp_path / "patched.yaml"
        target.write_text(yaml.safe_dump(_patched(fixture), sort_keys=False), encoding="utf-8")
        assert cli_main(["validate", str(target), "--format", "json"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["ok"] is False
        reported = {v["rule"] for v in report["violations"]}
        assert set(fixture["expect_rules"]) <= reported
        assert reported <= set(fixture["expect_rules"]) | set(fixture["allow_rules"])

    def test_stale_patch_raises(self, path: Path) -> None:
        """The leading test op fails on a scenario that no longer matches the patch."""
        fixture = _load_fixture(path)
        first = fixture["patch"][0]
        stale = _scenario()
        pointer = jsonpatch.JsonPointer(first["path"])
        pointer.set(stale, "stale-value")
        with pytest.raises(jsonpatch.JsonPatchTestFailed):
            jsonpatch.apply_patch(stale, fixture["patch"])
