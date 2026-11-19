"""Property test for the append-only env update (Requirements 13.1, 13.2, 13.3).

An existing ``.env`` is built line by line from comments, blank lines,
commented-out assignments, unquoted and quoted values (with ``#`` and spaces
inside the quotes), trailing comments, ``CAIRN_DB_PORT=55432`` and keys the
template lacks, joined with ``\\n`` or ``\\r\\n``, with or without a final
newline. A template is built from comments, blank lines, plain assignments
(including look-alikes of the placeholders) and ``change-me`` or
``user-provided`` assignments in the unquoted, single-quoted and double-quoted
forms with an optional trailing comment. Keys come mostly from a small shared
pool, so the two files overlap often, and a key may repeat in either file.

Each generated line carries the key it assigns and the text around its value,
so the expected suffix is built without the parser under test. Tokens come
from the real ``random_token``; the test records them to build the expectation.

**Validates: Requirements 13.1, 13.2, 13.3**
"""

# Feature: cairn-free-tier-retarget, Property 13: Appending to an environment file keeps every existing byte  # noqa: E501

import datetime as dt
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scripts import dev_env
from scripts.dev_env import MIN_SECRET_LENGTH, PLACEHOLDER, USER_PROVIDED

PLACEHOLDERS = (PLACEHOLDER, USER_PROVIDED)
# Printable ASCII only: str.splitlines also splits on \x0b, \x1c and Unicode
# separators, which a dotenv file never contains.
PRINTABLE = st.characters(min_codepoint=32, max_codepoint=126)
VALUE_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:*-"
KEY_POOL = (
    "CAIRN_DB_PORT",
    "CAIRN_DB_SSLMODE",
    "CAIRN_PROXY_TOKEN",
    "CAIRN_MANAGED_ADMIN_PASSWORD",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "OPENAI_API_KEY",
    "CAIRN_EMBEDDER",
    "export",
    "_X",
)
KEYS = st.one_of(
    st.sampled_from(KEY_POOL),
    st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True),
)
PREFIX_SPACING = st.sampled_from(["=", " = ", "= ", " ="])
EXPORT = st.sampled_from(["", "export "])
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class GenLine:
    """A generated line and the key it assigns (``None`` for comments and blanks).

    For a template placeholder, ``kind`` is ``secret`` or ``user`` and
    ``before`` and ``after`` are the text around the placeholder value.
    """

    text: str
    key: str | None = None
    kind: str = "plain"
    before: str = ""
    after: str = ""


@st.composite
def comment_lines(draw: st.DrawFn) -> GenLine:
    indent = draw(st.sampled_from(["", "  "]))
    return GenLine(f"{indent}#{draw(st.text(PRINTABLE, max_size=30))}")


@st.composite
def commented_assignments(draw: st.DrawFn) -> GenLine:
    """``# KEY=value``: a disabled setting, which does not count as present."""
    indent = draw(st.sampled_from(["", "  "]))
    space = draw(st.sampled_from(["", " "]))
    value = draw(st.text(VALUE_CHARS, max_size=10))
    return GenLine(f"{indent}#{space}{draw(KEYS)}={value}")


@st.composite
def blank_lines(draw: st.DrawFn) -> GenLine:
    return GenLine(draw(st.sampled_from(["", " ", "   "])))


@st.composite
def trailing_comment(draw: st.DrawFn) -> str:
    """Nothing, spaces, or spaces followed by a comment."""
    kind = draw(st.sampled_from(["none", "spaces", "comment"]))
    if kind == "none":
        return ""
    if kind == "spaces":
        return draw(st.sampled_from([" ", "   "]))
    return " # " + draw(st.text(PRINTABLE, max_size=20))


@st.composite
def plain_values(draw: st.DrawFn) -> str:
    """A value that is not a placeholder, followed by optional trailing text."""
    kind = draw(st.sampled_from(["plain", "quoted", "lookalike"]))
    if kind == "plain":
        value = draw(st.text(VALUE_CHARS, max_size=20).filter(lambda v: v not in PLACEHOLDERS))
        return value + draw(trailing_comment())
    if kind == "quoted":
        quote = draw(st.sampled_from(['"', "'"]))
        inner = draw(
            st.text(PRINTABLE, max_size=20).filter(
                lambda v: quote not in v and v not in PLACEHOLDERS
            )
        )
        return f"{quote}{inner}{quote}" + draw(trailing_comment())
    return draw(
        st.sampled_from(
            [
                "change-me-later",
                "change-me-later # change-me",
                '"user-provided-later"',
                "Change-Me",
                "user_provided",
                "change-me extra",
                '"user-provided"x',
                "'change-me",
                " # user-provided",
            ]
        )
    )


@st.composite
def plain_assignments(draw: st.DrawFn) -> GenLine:
    key = draw(KEYS)
    head = draw(EXPORT) + key + draw(PREFIX_SPACING)
    return GenLine(head + draw(plain_values()), key=key)


@st.composite
def placeholder_assignments(draw: st.DrawFn) -> GenLine:
    key = draw(KEYS)
    before = draw(EXPORT) + key + draw(PREFIX_SPACING)
    placeholder = draw(st.sampled_from(PLACEHOLDERS))
    quote = draw(st.sampled_from(["", '"', "'"]))
    after = draw(trailing_comment())
    kind = "secret" if placeholder == PLACEHOLDER else "user"
    text = f"{before}{quote}{placeholder}{quote}{after}"
    return GenLine(text, key=key, kind=kind, before=before, after=after)


EXISTING_LINE = st.one_of(
    comment_lines(),
    commented_assignments(),
    blank_lines(),
    plain_assignments(),
    st.just(GenLine("CAIRN_DB_PORT=55432", key="CAIRN_DB_PORT")),  # a local port override
)
TEMPLATE_LINE = st.one_of(
    comment_lines(),
    blank_lines(),
    plain_assignments(),
    placeholder_assignments(),
    placeholder_assignments(),
)


@st.composite
def existing_files(draw: st.DrawFn) -> tuple[str, list[GenLine]]:
    lines = draw(st.lists(EXISTING_LINE, max_size=15))
    separator = draw(st.sampled_from(["\n", "\r\n"]))
    text = separator.join(line.text for line in lines)
    final_newline: bool = draw(st.booleans())
    if lines and final_newline:
        text += separator
    return text, lines


@st.composite
def templates(draw: st.DrawFn) -> tuple[str, list[GenLine]]:
    lines = draw(st.lists(TEMPLATE_LINE, max_size=15))
    text = "\n".join(line.text for line in lines)
    return (text + "\n" if lines else text), lines


def recording_source() -> tuple[Callable[[], str], list[str]]:
    """The real token source, recording every token it hands out."""
    tokens: list[str] = []

    def source() -> str:
        token = dev_env.random_token()
        tokens.append(token)
        return token

    return source, tokens


def expected_append(existing_lines: list[GenLine], template_lines: list[GenLine]) -> list[GenLine]:
    """Template lines to append: first occurrence of each key the file lacks, in order."""
    present = {line.key for line in existing_lines if line.key is not None}
    appended: list[GenLine] = []
    for line in template_lines:
        if line.key is None or line.key in present:
            continue
        present.add(line.key)
        appended.append(line)
    return appended


def assert_appended_lines(
    rendered: list[str], expected: list[GenLine], tokens: list[str], existing: str
) -> None:
    """Each appended line is rendered as its kind demands (Requirement 13.2)."""
    assert len(rendered) == len(expected)
    pending = iter(tokens)
    for out, line in zip(rendered, expected, strict=True):
        if line.kind == "secret":
            token = next(pending)
            assert out == f"{line.before}{token}{line.after}"
            assert len(token) >= MIN_SECRET_LENGTH
            assert token not in PLACEHOLDERS
            assert TOKEN_RE.fullmatch(token)
            assert token not in existing  # fresh, not copied from the file
        elif line.kind == "user":
            assert out == f"{line.before}{line.after}"  # an empty value
            assert USER_PROVIDED not in out.removesuffix(line.after)
        else:
            assert out == line.text  # template defaults are copied verbatim
    assert next(pending, None) is None  # no token was drawn for anything else
    assert len(set(tokens)) == len(tokens)


@given(existing_file=existing_files(), template_file=templates(), today=st.dates())
def test_add_missing_keeps_every_existing_byte(
    existing_file: tuple[str, list[GenLine]],
    template_file: tuple[str, list[GenLine]],
    today: dt.date,
) -> None:
    """Property 13: appending to an environment file keeps every existing byte.

    Feature: cairn-free-tier-retarget, Property 13 (see the module comment for the full tag).
    """
    existing, existing_lines = existing_file
    template, template_lines = template_file
    source, tokens = recording_source()

    suffix, keys = dev_env.add_missing(existing, template, source, today)
    result = existing + suffix
    expected = expected_append(existing_lines, template_lines)

    # 13.1: the result starts with the existing text, byte for byte.
    assert result.startswith(existing)
    assert result.encode("utf-8")[: len(existing.encode("utf-8"))] == existing.encode("utf-8")

    # 13.1: every absent template key is appended once, in template order, and
    # no key the file already assigns is appended again.
    present = {line.key for line in existing_lines if line.key is not None}
    assert keys == [line.key for line in expected]
    assert not present & set(keys)
    template_keys = {line.key for line in template_lines if line.key is not None}
    assert template_keys <= dev_env.env_keys(result)

    if not expected:
        # 13.3: nothing is missing, so nothing is appended.
        assert suffix == ""
        assert tokens == []
    else:
        head = "\n" if existing and not existing.endswith(("\n", "\r")) else ""
        assert suffix.startswith(head)
        assert suffix.endswith("\n")
        comment, *rendered = suffix.removeprefix(head).removesuffix("\n").split("\n")
        assert comment.startswith(f"# Added by make env-update on {today.isoformat()}:")
        # 13.2: fresh tokens for change-me keys, empty values for user-provided keys.
        assert_appended_lines(rendered, expected, tokens, existing)

    # 13.3: a second application to the result adds nothing.
    again, again_source_tokens = recording_source()
    assert dev_env.add_missing(result, template, again, today) == ("", [])
    assert again_source_tokens == []


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(existing_file=existing_files(), template_file=templates(), today=st.dates())
def test_update_env_appends_to_the_file_only(
    tmp_path: Path,
    existing_file: tuple[str, list[GenLine]],
    template_file: tuple[str, list[GenLine]],
    today: dt.date,
) -> None:
    """Property 13 through files: ``update_env`` only appends, and a rerun is a no-op.

    Feature: cairn-free-tier-retarget, Property 13 (see the module comment for the full tag).
    """
    existing, existing_lines = existing_file
    template, template_lines = template_file
    template_path = tmp_path / "env.example"
    output_path = tmp_path / "env"  # a temporary file, never the repository's .env
    template_path.write_bytes(template.encode("utf-8"))
    output_path.write_bytes(existing.encode("utf-8"))
    os.chmod(output_path, 0o600)
    before = existing.encode("utf-8")
    source, tokens = recording_source()

    keys = dev_env.update_env(template_path, output_path, source, today)
    after = output_path.read_bytes()
    expected = expected_append(existing_lines, template_lines)

    # 13.1: the file still starts with every original byte, and only the
    # absent template keys were added; the mode is kept.
    assert after.startswith(before)
    assert keys == [line.key for line in expected]
    assert output_path.stat().st_mode & 0o777 == 0o600
    if expected:
        suffix = after[len(before) :].decode("utf-8")
        head = "\n" if existing and not existing.endswith(("\n", "\r")) else ""
        comment, *rendered = suffix.removeprefix(head).removesuffix("\n").split("\n")
        assert comment.startswith(f"# Added by make env-update on {today.isoformat()}:")
        assert_appended_lines(rendered, expected, tokens, existing)
    else:
        assert after == before

    # 13.3: a second run finds every key and leaves the file untouched.
    stat = output_path.stat()
    assert dev_env.update_env(template_path, output_path, source, today) == []
    assert output_path.read_bytes() == after
    assert output_path.stat().st_mtime_ns == stat.st_mtime_ns
