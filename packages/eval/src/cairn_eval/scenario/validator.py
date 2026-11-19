"""The scenario validator: structure first, then semantic rules (design §12).

Pipeline for :func:`validate`:

1. **SCH** the raw data against the committed JSON Schema (Draft 2020-12, format checks).
   Every schema error becomes one violation. If there is any, validation stops here,
   because the semantic rules need a typed :class:`Scenario`.
2. **SCH** build the :class:`Scenario` models. Checks that only the models make (a
   principal with both or neither of team and person, a phrase canary without a phrase,
   a malformed timestamp) are reported as SCH too.
3. The semantic rules of :mod:`cairn_eval.scenario.rules` (ID-1 ... Q-3).

All violations of a stage are collected (no fail-fast). The result is sorted by rule id,
then pointer, with no duplicates, and depends only on the input (requirement 13.24).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as SchemaError
from jsonschema.exceptions import best_match
from pydantic import ValidationError

from cairn_eval.scenario.loader import load_scenario
from cairn_eval.scenario.model import Scenario
from cairn_eval.scenario.rules import check_semantics
from cairn_eval.scenario.violation import Violation, finalize, pointer


@cache
def _schema_validator() -> Draft202012Validator:
    # Imported here, not at module level: ``cairn_eval.scenario.schema`` must stay out of
    # the package import so that ``python -m cairn_eval.scenario.schema`` runs cleanly.
    from cairn_eval.scenario.schema import generate_schema

    return Draft202012Validator(generate_schema(), format_checker=FormatChecker())


def check_schema(raw: object) -> list[Violation]:
    """SCH from the JSON Schema: one violation per schema error, sorted and deduplicated."""
    found: list[Violation] = []
    for error in _schema_validator().iter_errors(raw):
        # For anyOf/oneOf the useful error is nested; best_match digs it out.
        deepest: SchemaError = best_match([error]) or error
        found.append(Violation("SCH", pointer(*deepest.absolute_path), deepest.message))
    return finalize(found)


def build_scenario(raw: object) -> tuple[Scenario | None, list[Violation]]:
    """Build the models. Returns the scenario, or ``None`` with SCH violations."""
    try:
        return Scenario.model_validate(raw), []
    except ValidationError as exc:
        return None, finalize(
            Violation("SCH", pointer(*error["loc"]), error["msg"]) for error in exc.errors()
        )


def validate(raw: object) -> list[Violation]:
    """Validate raw scenario data (any YAML value). Pure and deterministic.

    Returns the sorted violations; an empty list means every rule passes.
    """
    schema_problems = check_schema(raw)
    if schema_problems:
        return schema_problems
    scenario, model_problems = build_scenario(raw)
    if scenario is None:
        return model_problems
    return check_semantics(scenario)


def validate_file(path: str | Path) -> list[Violation]:
    """Load a scenario file and validate it.

    Raises :class:`cairn_eval.scenario.loader.ScenarioLoadError` if the file is missing,
    unreadable or not valid YAML.
    """
    return validate(load_scenario(path))


def summarize(raw: object) -> dict[str, int]:
    """Collection sizes of a scenario that passed validation, for the CLI summary."""
    scenario = Scenario.model_validate(raw)
    counts: dict[str, Any] = {
        "people": scenario.people,
        "teams": scenario.teams,
        "repos": scenario.repos,
        "channels": scenario.channels,
        "sources": scenario.sources,
        "decisions": scenario.decisions,
        "twists": scenario.twists,
        "canaries": scenario.canaries,
        "questions": scenario.questions,
    }
    return {name: len(items) for name, items in counts.items()}
