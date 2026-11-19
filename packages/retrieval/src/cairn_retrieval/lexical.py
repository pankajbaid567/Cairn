"""Lexical query form (PD1-25).

``websearch_to_tsquery`` ANDs bare words, which drops most rows for a natural question. So the
registered query receives one string of the distinct lowercase word tokens joined with `` or ``.
``english`` stems prose; ``simple`` keeps identifiers such as ``t3_5``.
"""

from __future__ import annotations

import re
from typing import Final

MAX_TERMS: Final = 32
_WORD: Final = re.compile(r"\w+")


def lexical_terms(question: str) -> tuple[str, ...]:
    """Distinct casefolded ``\\w+`` tokens in first-appearance order, ``or`` dropped, at most
    :data:`MAX_TERMS`. Empty when the question has no word character."""
    seen: dict[str, None] = {}
    for token in _WORD.findall(question.casefold()):
        if token != "or":  # noqa: S105
            seen.setdefault(token)
        if len(seen) >= MAX_TERMS:
            break
    return tuple(seen)


def lexical_query_text(terms: tuple[str, ...] | list[str]) -> str:
    return " or ".join(terms)
