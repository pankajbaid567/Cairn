"""Timestamp-free YAML loading for scenario files (design §11, §12).

``yaml.SafeLoader`` follows YAML 1.1, which would change the meaning of a scenario
file in three ways. :class:`ScenarioLoader` switches them off:

* Unquoted ``2026-03-07`` and ``2026-02-18T09:12:00Z`` become ``date`` and ``datetime``
  objects. Here they stay strings, so the JSON Schema (which only knows strings) sees
  what the author wrote.
* ``on``, ``off``, ``yes``, ``no`` become booleans. That would turn the timeline key
  ``on:`` into ``True``. Here only ``true`` and ``false`` (any case) are booleans.
* Duplicate mapping keys silently keep the last value. Here they are an error.

Everything else is ``SafeLoader`` behavior: no arbitrary Python objects can be built.
"""

from __future__ import annotations

import re
from collections.abc import Hashable
from pathlib import Path
from typing import Any, Final

import yaml

_TIMESTAMP_TAG: Final = "tag:yaml.org,2002:timestamp"
_BOOL_TAG: Final = "tag:yaml.org,2002:bool"
_BOOL_RE: Final = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


class ScenarioLoadError(Exception):
    """The scenario file is unreadable or is not valid YAML (validator exit status 2).

    ``line`` and ``column`` are 1-based and present when the YAML parser knows them.
    """

    def __init__(
        self,
        message: str,
        *,
        source: str,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.message = message
        self.source = source
        self.line = line
        self.column = column
        where = f"{source}:{line}:{column}" if line is not None else source
        super().__init__(f"{where}: {message}")


class ScenarioLoader(yaml.SafeLoader):
    """``SafeLoader`` without timestamps, YAML 1.1 booleans and duplicate keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        self.flatten_mapping(node)
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if isinstance(key, Hashable):
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


# Copy the resolver table so SafeLoader (and every other user of PyYAML) is untouched.
ScenarioLoader.yaml_implicit_resolvers = {
    first_char: [(tag, rx) for tag, rx in resolvers if tag not in {_TIMESTAMP_TAG, _BOOL_TAG}]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
ScenarioLoader.add_implicit_resolver(_BOOL_TAG, _BOOL_RE, list("tTfF"))


def load_scenario_text(text: str, source: str = "<string>") -> object:
    """Parse scenario YAML text into plain Python data (dicts, lists, strings, numbers).

    Raises :class:`ScenarioLoadError` on a syntax error, a duplicate key or a file with
    more than one YAML document. An empty document returns ``None``.
    """
    try:
        # ScenarioLoader derives from SafeLoader, so this cannot construct Python objects.
        return yaml.load(text, Loader=ScenarioLoader)  # noqa: S506
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        message = " ".join(part for part in (exc.context, exc.problem) if part)
        raise ScenarioLoadError(
            message or "invalid YAML",
            source=source,
            line=mark.line + 1 if mark else None,
            column=mark.column + 1 if mark else None,
        ) from exc
    except yaml.YAMLError as exc:
        raise ScenarioLoadError(str(exc), source=source) from exc


def load_scenario(path: str | Path) -> object:
    """Read and parse a scenario file into raw data (use ``Scenario.model_validate`` after).

    Raises :class:`ScenarioLoadError` if the file cannot be read or parsed.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ScenarioLoadError(f"cannot read file: {exc}", source=str(file)) from exc
    return load_scenario_text(text, source=str(file))
