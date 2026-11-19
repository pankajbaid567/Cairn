"""The pytest and Hypothesis setup every test relies on (design §2, Requirement 2.4)."""

import os

import pytest
from hypothesis import settings

LAYER_MARKERS = {"unit", "integration", "matrix", "smoke"}


def test_layer_markers_are_registered_and_enforced(pytestconfig: pytest.Config) -> None:
    lines: list[str] = pytestconfig.getini("markers")
    registered = {line.split(":", 1)[0].strip() for line in lines}
    assert registered >= LAYER_MARKERS
    assert pytestconfig.getini("strict_markers") is True


def test_hypothesis_profiles_set_the_example_counts() -> None:
    assert settings.get_profile("dev").max_examples == 100
    assert settings.get_profile("ci").max_examples == 200


def test_active_hypothesis_profile_follows_the_environment() -> None:
    active = settings.get_current_profile_name()
    requested = os.environ.get("HYPOTHESIS_PROFILE")
    if requested is not None:
        assert active == requested
    assert active in {"dev", "ci"}
    assert settings().max_examples == settings.get_profile(active).max_examples
