"""YAML front matter (design §5). Dates stay strings: the loader has no timestamp type."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

import yaml

from cairn_connectors.documents import FormatError

_TIMESTAMP_TAG: Final = "tag:yaml.org,2002:timestamp"


class _Loader(yaml.SafeLoader):
    """``SafeLoader`` without the timestamp type, so ``2026-03-07`` stays a string."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != _TIMESTAMP_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

_FENCE: Final = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


def parse_front_matter(text: str, *, source: str = "<text>") -> tuple[dict[str, Any], str]:
    """``(metadata, body)``. A file without front matter returns ``({}, text)``.

    Raises ``FormatError`` naming ``source`` when the block is not a YAML mapping.
    """
    match = _FENCE.match(text)
    if match is None:
        return {}, text
    try:
        data = yaml.load(match.group(1), Loader=_Loader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise FormatError(f"{source}: front matter is not valid YAML") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FormatError(f"{source}: front matter must be a mapping")
    return data, text[match.end() :]


def print_front_matter(meta: Mapping[str, object], body: str) -> str:
    """The inverse of :func:`parse_front_matter` for mappings of plain values.

    Keys keep the order given (``sort_keys=False``), so a renderer controls the layout.
    """
    block = yaml.safe_dump(dict(meta), sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"---\n{block}\n---\n{body}"
