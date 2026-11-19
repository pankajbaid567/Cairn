"""Kestrel Pay scenario tooling (design §11, §12).

* ``model``      typed models of scenario YAML v1 (the source of truth for the format)
* ``loader``     timestamp-free YAML loading
* ``canary``     deterministic canary tokens
* ``oracle``     the lifecycle ``TRANSITIONS`` and the as-of oracle ``state_as_of``
* ``access``     who can read a scope on a date
* ``rules``      the semantic validator rules (ID-1 ... Q-3), one pure function each
* ``validator``  ``validate``: JSON Schema, models, then the rules
* ``schema``, ``cli``  import explicitly; they are not re-exported here so that
              ``python -m cairn_eval.scenario.schema`` and ``... .cli`` run cleanly
"""

from cairn_eval.scenario.canary import CANARY_FAMILIES, CANARY_RE, PLACEHOLDER_RE, canary_token
from cairn_eval.scenario.loader import (
    ScenarioLoader,
    ScenarioLoadError,
    load_scenario,
    load_scenario_text,
)
from cairn_eval.scenario.model import Scenario
from cairn_eval.scenario.oracle import (
    TRANSITIONS,
    recorded_time,
    state_as_of,
    valid_time,
)
from cairn_eval.scenario.validator import validate, validate_file
from cairn_eval.scenario.violation import Violation

__all__ = [
    "CANARY_FAMILIES",
    "CANARY_RE",
    "PLACEHOLDER_RE",
    "TRANSITIONS",
    "Scenario",
    "ScenarioLoadError",
    "ScenarioLoader",
    "Violation",
    "canary_token",
    "load_scenario",
    "load_scenario_text",
    "recorded_time",
    "state_as_of",
    "valid_time",
    "validate",
    "validate_file",
]
