"""Property test for the env generator (Requirement 22.2).

Templates are built line by line from comments, blank lines, plain assignments
(including look-alikes of the placeholder) and secret assignments in the
unquoted, single-quoted and double-quoted forms with an optional trailing
comment. Each generated line carries the output it must produce, so the
expectation does not depend on the parser under test.

**Validates: Requirements 22.2**
"""

# Feature: cairn-phase-0-foundations, Property 19: Env generation fills secrets and keeps everything else  # noqa: E501

import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass

from hypothesis import given
from hypothesis import strategies as st

from scripts import dev_env
from scripts.dev_env import PLACEHOLDER, USER_PROVIDED

PLACEHOLDERS = (PLACEHOLDER, USER_PROVIDED)

# Printable ASCII only: str.splitlines also splits on \x0b, \x1c and Unicode
# separators, which a dotenv file never contains.
PRINTABLE = st.characters(min_codepoint=32, max_codepoint=126)
VALUE_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:*-"

KEYS = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True)
PREFIX_SPACING = st.sampled_from(["=", " = ", "= ", " ="])
EXPORT = st.sampled_from(["", "export "])


@dataclass(frozen=True)
class GenLine:
    """A template line, the key it assigns (if any), and whether it is a secret.

    For a secret, ``before`` and ``after`` are the text around the placeholder
    value; the rendered line is ``before + token + after``.
    """

    text: str
    key: str | None = None
    secret: bool = False
    before: str = ""
    after: str = ""


def counter_source() -> Callable[[], str]:
    numbers = itertools.count(1)
    return lambda: f"token-{next(numbers):022d}"


@st.composite
def comment_lines(draw: st.DrawFn) -> GenLine:
    indent = draw(st.sampled_from(["", "  "]))
    text = draw(st.text(PRINTABLE, max_size=30))
    return GenLine(f"{indent}#{text}")


@st.composite
def blank_lines(draw: st.DrawFn) -> GenLine:
    return GenLine(draw(st.sampled_from(["", " ", "   "])))


@st.composite
def trailing_comment(draw: st.DrawFn) -> str:
    """Nothing, spaces, or spaces followed by a comment (the allowed `rest`)."""
    kind = draw(st.sampled_from(["none", "spaces", "comment"]))
    if kind == "none":
        return ""
    if kind == "spaces":
        return draw(st.sampled_from([" ", "   "]))
    return " # " + draw(st.text(PRINTABLE, max_size=20))


@st.composite
def plain_lines(draw: st.DrawFn) -> GenLine:
    """Assignments that are not secrets, including look-alikes of the placeholder."""
    key = draw(KEYS)
    head = draw(EXPORT) + key + draw(PREFIX_SPACING)
    kind = draw(st.sampled_from(["plain", "quoted", "lookalike"]))
    if kind == "plain":
        value = draw(st.text(VALUE_CHARS, max_size=20).filter(lambda v: v not in PLACEHOLDERS))
        rest = draw(trailing_comment())
    elif kind == "quoted":
        quote = draw(st.sampled_from(['"', "'"]))
        inner = draw(
            st.text(PRINTABLE, max_size=20).filter(
                lambda v: quote not in v and v not in PLACEHOLDERS
            )
        )
        value, rest = f"{quote}{inner}{quote}", draw(trailing_comment())
    else:
        value, rest = draw(
            st.sampled_from(
                [
                    ("change-me-later", ""),
                    ("change-me-later", " # change-me"),
                    ('"change-me-later"', ""),
                    ("Change-Me", ""),
                    ("change_me", ""),
                    ("change-me", " extra"),
                    ('"change-me"', "x"),
                    ("'change-me", ""),
                    ("", " # change-me"),
                ]
            )
        )
    return GenLine(head + value + rest, key=key)


@st.composite
def secret_lines(draw: st.DrawFn) -> GenLine:
    key = draw(KEYS)
    before = draw(EXPORT) + key + draw(PREFIX_SPACING)
    value = draw(st.sampled_from([PLACEHOLDER, f'"{PLACEHOLDER}"', f"'{PLACEHOLDER}'"]))
    after = draw(trailing_comment())
    return GenLine(before + value + after, key=key, secret=True, before=before, after=after)


ANY_LINE = st.one_of(comment_lines(), blank_lines(), plain_lines(), secret_lines())
# The last line is never blank, so the template survives splitlines() unchanged
# whether or not it ends with a newline.
LAST_LINE = st.one_of(comment_lines(), plain_lines(), secret_lines())


@given(
    body=st.lists(ANY_LINE, max_size=25),
    last=LAST_LINE,
    final_newline=st.booleans(),
)
def test_env_generation_fills_secrets_and_keeps_everything_else(
    body: list[GenLine], last: GenLine, final_newline: bool
) -> None:
    """Property 19: env generation fills secrets and keeps everything else.

    Feature: cairn-phase-0-foundations, Property 19 (see the module comment for the full tag).
    """
    lines = [*body, last]
    template = "\n".join(line.text for line in lines) + ("\n" if final_newline else "")

    # The expected output, built from what the generator knows about each line.
    expected_tokens = [f"token-{n:022d}" for n in range(1, sum(line.secret for line in lines) + 1)]
    pending = iter(expected_tokens)
    expected_lines = [
        f"{line.before}{next(pending)}{line.after}" if line.secret else line.text for line in lines
    ]

    calls = 0
    source = counter_source()

    def counting_source() -> str:
        nonlocal calls
        calls += 1
        return source()

    output = dev_env.render(template, counting_source)

    # One token per placeholder line, drawn in file order, and no others.
    assert calls == len(expected_tokens)
    # Same line count; every line is the expected one. This covers: tokens land
    # in file order, comments, blank lines, keys and non-secret values are
    # byte-for-byte unchanged, and text around a secret's value is preserved.
    assert output.splitlines() == expected_lines
    # Exactly one trailing newline, whether or not the template had one.
    assert output.endswith("\n")
    assert not output.endswith("\n\n")  # the last generated line is never blank
    assert output == "\n".join(expected_lines) + "\n"

    # No placeholder remains where a secret was: the output holds no secrets,
    # and each secret line now holds its token as the value.
    assert dev_env.secret_keys(output) == []
    for line, out_line in zip(lines, output.splitlines(), strict=True):
        if line.secret:
            assert re.search(r"token-\d{22}", out_line)
            assert PLACEHOLDER not in out_line.removesuffix(line.after)

    # secret_keys agrees with the lines generated as secrets, in file order.
    assert dev_env.secret_keys(template) == [line.key for line in lines if line.secret]
