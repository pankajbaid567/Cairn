"""Span-preserving chunker (design §7; PD1-10).

Offsets are Python code point offsets into the normalized revision text, half-open
``[start, end)``, and ``chunk.text == revision_text[start:end]`` always. Chunks never cross a
Markdown section or a chat message. Whitespace between units belongs to no chunk.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

TARGET_TOKENS: Final = 300
HARD_MAX_TOKENS: Final = 450
_TOKENS_PER_WORD: Final = 1.3

_HEADING: Final = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    start: int
    end: int
    text: str
    heading_path: tuple[str, ...]
    token_estimate: int


def estimate_tokens(text: str) -> int:
    """``ceil(1.3 * word count)`` (Estimate), at least 1 for non-empty text."""
    words = len(text.split())
    return max(1, math.ceil(_TOKENS_PER_WORD * words)) if words else 0


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    section: int
    path: tuple[str, ...]


def _lines(text: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    pos = 0
    for line in text.split("\n"):
        out.append((pos, pos + len(line), line))
        pos += len(line) + 1
    return out


def _markdown_units(text: str) -> list[_Unit]:
    units: list[_Unit] = []
    stack: list[tuple[int, str]] = []
    section = 0
    para_start: int | None = None
    para_end = 0
    path: tuple[str, ...] = ()

    def flush() -> None:
        nonlocal para_start
        if para_start is not None:
            units.append(_Unit(para_start, para_end, section, path))
            para_start = None

    for start, end, line in _lines(text):
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading.group(2).strip()))
            section += 1
            path = tuple(title for _, title in stack)
            units.append(_Unit(start, end, section, path))
        elif line.strip():
            if para_start is None:
                para_start = start
            para_end = end
        else:
            flush()
    flush()
    return units


def _chat_units(text: str) -> list[_Unit]:
    return [_Unit(s, e, 0, ()) for s, e, line in _lines(text) if line.strip()]


def _split_unit(text: str, unit: _Unit, hard_max: int, target: int) -> list[_Unit]:
    """A unit above ``hard_max`` splits at sentence ends, then at whitespace."""
    if estimate_tokens(text[unit.start : unit.end]) <= hard_max:
        return [unit]
    pieces: list[tuple[int, int]] = []
    pos = unit.start
    body = text[unit.start : unit.end]
    for match in _SENTENCE_END.finditer(body):
        pieces.append((pos, unit.start + match.start()))
        pos = unit.start + match.end()
    pieces.append((pos, unit.end))
    out: list[_Unit] = []
    for start, end in pieces:
        if estimate_tokens(text[start:end]) <= hard_max:
            out.append(_Unit(start, end, unit.section, unit.path))
            continue
        max_words = max(1, int(target / _TOKENS_PER_WORD))
        words = list(re.finditer(r"\S+", text[start:end]))
        for i in range(0, len(words), max_words):
            group = words[i : i + max_words]
            out.append(
                _Unit(start + group[0].start(), start + group[-1].end(), unit.section, unit.path)
            )
    return out


def _pack(text: str, units: list[_Unit], target: int, hard_max: int) -> list[Chunk]:
    expanded: list[_Unit] = []
    for unit in units:
        expanded.extend(_split_unit(text, unit, hard_max, target))
    chunks: list[Chunk] = []
    current: list[_Unit] = []

    def close() -> None:
        if not current:
            return
        start, end = current[0].start, current[-1].end
        body = text[start:end]
        chunks.append(
            Chunk(len(chunks), start, end, body, current[0].path, max(1, estimate_tokens(body)))
        )
        current.clear()

    for unit in expanded:
        if current and unit.section != current[-1].section:
            close()
        if current:
            joined = text[current[0].start : unit.end]
            if estimate_tokens(joined) > target:
                close()
        current.append(unit)
    close()
    return chunks


def chunk_markdown(
    text: str, *, target: int = TARGET_TOKENS, hard_max: int = HARD_MAX_TOKENS
) -> list[Chunk]:
    """Split Markdown at headings and paragraphs; pack units up to ``target`` tokens."""
    return _pack(text, _markdown_units(text), target, hard_max)


def chunk_chat(
    text: str, *, target: int = TARGET_TOKENS, hard_max: int = HARD_MAX_TOKENS
) -> list[Chunk]:
    """Split chat text (one message per line); a message is never split unless it is huge."""
    return _pack(text, _chat_units(text), target, hard_max)
