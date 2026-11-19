"""The committed JSON Schema for scenario files (``scenario-v1.schema.json``).

The schema is generated from :class:`cairn_eval.scenario.model.Scenario`; never edit
the file by hand. After changing a model, regenerate it:

    uv run python -m cairn_eval.scenario.schema          # write the file
    uv run python -m cairn_eval.scenario.schema --check  # exit 1 if it is out of date

A unit test fails when the committed file differs from :func:`render_schema`
(requirement 13.28). Scenario YAML files point editors at it with a
``# yaml-language-server: $schema=...`` comment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

from cairn_eval.scenario.model import Scenario

SCHEMA_FILENAME: Final = "scenario-v1.schema.json"
JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_TITLE: Final = "Kestrel Pay scenario, schema version 1"


def schema_path() -> Path:
    """Location of the committed schema file, next to this module."""
    return Path(__file__).with_name(SCHEMA_FILENAME)


def generate_schema() -> dict[str, Any]:
    """Build the schema from the models (validation mode, aliases as in the YAML)."""
    generated = Scenario.model_json_schema(mode="validation", by_alias=True)
    body = {key: value for key, value in generated.items() if key != "title"}
    return {"$schema": JSON_SCHEMA_DIALECT, "title": SCHEMA_TITLE, **body}


def render_schema() -> str:
    """The exact text of the committed file: 2-space JSON, key order kept, final newline."""
    return json.dumps(generate_schema(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check scenario-v1.schema.json.")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the committed file is out of date"
    )
    args = parser.parse_args(argv)
    target = schema_path()
    expected = render_schema()
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current != expected:
            print(f"{target} is out of date; run: python -m cairn_eval.scenario.schema")
            return 1
        return 0
    target.write_text(expected, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
