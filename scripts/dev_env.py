"""Write a local ``.env`` from ``.env.example`` with random secrets (``make env``).

Design §10 and Requirement 22 (Phase 0); free-tier design §6 and Requirement 13.

The template is read line by line. Comments, blank lines, key order and every
non-secret value are copied unchanged. A value is a placeholder when it is
exactly ``change-me`` or ``user-provided`` (unquoted, or in single or double
quotes, with nothing but an optional comment after it):

- ``change-me`` is a generated secret. Each one gets a fresh random URL-safe
  token (so the quotes around a quoted placeholder are dropped).
- ``user-provided`` is a value only the user can supply (an API token, a
  managed database host). It is written as an empty value, which the settings
  treat as unset, so a placeholder string never reaches a provider.

The generator never prints a secret.

Two modes:

- default (``make env``): create the output with mode 0600 from the template,
  and refuse to overwrite an existing file, so re-running it can never rotate
  secrets under a running stack.
- ``--add-missing`` (``make env-update``): append a dated comment and every
  template key that the output lacks, rendered as above. Existing bytes are
  never rewritten, reordered or removed: the file is opened for appending
  only. A file that already holds every key is left untouched. A missing
  output is created as in the default mode.

``--template`` and ``--output`` (alias ``--out``) choose the files, so
``make env-neon`` can write ``.env.neon`` from ``.env.neon.example``.

Exit status: 0 written, appended or nothing to append; 1 output already exists
(default mode only); 2 template or output unreadable or invalid. Standard
library only, so it runs before ``uv sync``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER = "change-me"
USER_PROVIDED = "user-provided"
MIN_SECRET_LENGTH = 24  # characters; token_urlsafe(24) yields 32
TOKEN_BYTES = 24
ENV_FILE_MODE = 0o600
REPO_ROOT = Path(__file__).resolve().parent.parent

# KEY=value lines. The value is a quoted string or a run of non-space,
# non-# characters; whatever follows (spaces, a comment, more words) is `rest`.
_ASSIGNMENT = re.compile(
    r"""^(?P<prefix>\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*)
        (?P<value>"[^"]*"|'[^']*'|[^#\s]*)
        (?P<rest>.*)$""",
    re.VERBOSE,
)
_KEY = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")

TokenSource = Callable[[], str]


class EnvTemplateError(ValueError):
    """The template cannot be turned into an env file."""


@dataclass(frozen=True)
class Line:
    """One template line.

    ``secret`` is True when its value is the ``change-me`` placeholder and
    ``user_provided`` when it is the ``user-provided`` placeholder.
    """

    text: str
    key: str | None = None
    secret: bool = False
    user_provided: bool = False


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_line(text: str) -> Line:
    match = _ASSIGNMENT.match(text)
    key_match = _KEY.match(text)
    if match is None or key_match is None or text.lstrip().startswith("#"):
        return Line(text)
    rest = match["rest"].strip()
    bare = rest == "" or rest.startswith("#")
    value = _unquote(match["value"])
    return Line(
        text,
        key_match["key"],
        secret=bare and value == PLACEHOLDER,
        user_provided=bare and value == USER_PROVIDED,
    )


def parse_template(template: str) -> list[Line]:
    """Split a template into lines, marking which ones hold a placeholder."""
    return [_parse_line(text) for text in template.splitlines()]


def secret_keys(template: str) -> list[str]:
    """Keys of the template that hold the ``change-me`` placeholder, in file order."""
    return [line.key for line in parse_template(template) if line.secret and line.key]


def user_provided_keys(template: str) -> list[str]:
    """Keys of the template that hold the ``user-provided`` placeholder, in file order."""
    return [line.key for line in parse_template(template) if line.user_provided and line.key]


def _render_line(line: Line, token_source: TokenSource) -> str:
    if not (line.secret or line.user_provided):
        return line.text
    match = _ASSIGNMENT.match(line.text)
    if match is None:  # unreachable: _parse_line only marks matching lines
        raise EnvTemplateError("internal error: placeholder line does not parse")
    if line.user_provided:
        return f"{match['prefix']}{match['rest']}"
    token = token_source()
    _check_token(token)
    return f"{match['prefix']}{token}{match['rest']}"


def render(template: str, token_source: TokenSource) -> str:
    """Return the template with each placeholder filled in.

    ``change-me`` becomes ``token_source()`` and ``user-provided`` becomes an
    empty value. Pure apart from the token source. Raises ``EnvTemplateError``
    if the source returns a value that is too short, equals the placeholder,
    or contains characters that would need quoting in a dotenv file.
    """
    out = [_render_line(line, token_source) for line in parse_template(template)]
    # Keep the template's trailing newline convention: always end with one.
    return "\n".join(out) + "\n"


def env_keys(content: str) -> set[str]:
    """Keys assigned in an env file (commented-out assignments do not count)."""
    keys: set[str] = set()
    for text in content.splitlines():
        if text.lstrip().startswith("#"):
            continue
        match = _KEY.match(text)
        if match is not None:
            keys.add(match["key"])
    return keys


def missing_lines(existing: str, template: str, token_source: TokenSource) -> list[Line]:
    """Template assignments whose key ``existing`` lacks, in template order, once each."""
    present = env_keys(existing)
    missing: list[Line] = []
    for line in parse_template(template):
        if line.key is None or line.key in present:
            continue
        present.add(line.key)  # a key repeated in the template is appended once
        missing.append(Line(_render_line(line, token_source), line.key))
    return missing


def add_missing(
    existing: str, template: str, token_source: TokenSource, today: dt.date
) -> tuple[str, list[str]]:
    """Return the text to append to ``existing`` and the keys it adds.

    The suffix is empty when every template key is present. Otherwise it is a
    newline (only if ``existing`` does not end with one), a comment dated
    ``today``, and one rendered line per missing key. ``existing + suffix``
    therefore starts with ``existing`` byte for byte.
    """
    lines = missing_lines(existing, template, token_source)
    if not lines:
        return "", []
    head = "\n" if existing and not existing.endswith(("\n", "\r")) else ""
    comment = f"# Added by make env-update on {today.isoformat()}: keys missing from the template."
    suffix = head + "\n".join([comment, *(line.text for line in lines)]) + "\n"
    return suffix, [line.key for line in lines if line.key]


def _check_token(token: str) -> None:
    if len(token) < MIN_SECRET_LENGTH:
        raise EnvTemplateError(f"secret source returned fewer than {MIN_SECRET_LENGTH} characters")
    if token in (PLACEHOLDER, USER_PROVIDED):
        raise EnvTemplateError("secret source returned a placeholder")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise EnvTemplateError("secret source returned characters that need quoting")


def random_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _read_template(template_path: Path) -> str:
    try:
        return template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvTemplateError(f"cannot read {template_path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise EnvTemplateError(f"{template_path} is not UTF-8") from exc


def write_env(
    template_path: Path, output_path: Path, token_source: TokenSource = random_token
) -> int:
    """Create ``output_path`` from the template. Returns the number of secrets generated.

    Raises ``FileExistsError`` if the output exists (a dangling symlink counts),
    and never touches an existing file. The file is created exclusively with
    mode 0600, so there is no window in which it is world-readable.
    """
    if os.path.lexists(output_path):
        raise FileExistsError(output_path)
    template = _read_template(template_path)
    content = render(template, token_source)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(output_path, flags, ENV_FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), ENV_FILE_MODE)  # exact mode, whatever the umask
            handle.write(content)
    except BaseException:
        output_path.unlink(missing_ok=True)  # never leave a half-written .env behind
        raise
    return len(secret_keys(template))


def update_env(
    template_path: Path,
    output_path: Path,
    token_source: TokenSource = random_token,
    today: dt.date | None = None,
) -> list[str]:
    """Append the template keys that ``output_path`` lacks. Returns the appended keys.

    The output is opened for appending only (never truncated, never through a
    symlink), so no existing byte can change. Nothing is written when no key
    is missing. Raises ``EnvTemplateError`` when a file cannot be read or
    opened (including a missing output).
    """
    template = _read_template(template_path)
    try:
        existing = output_path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise EnvTemplateError(f"cannot read {output_path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise EnvTemplateError(f"{output_path} is not UTF-8; left unchanged") from exc
    suffix, keys = add_missing(existing, template, token_source, today or dt.date.today())
    if not keys:
        return []
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(output_path, flags)
    except OSError as exc:
        raise EnvTemplateError(f"cannot open {output_path} for appending: {exc.strerror}") from exc
    with os.fdopen(fd, "ab") as handle:
        handle.write(suffix.encode("utf-8"))
    return keys


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--template", type=Path, default=REPO_ROOT / ".env.example")
    parser.add_argument("--output", "--out", dest="output", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument(
        "--add-missing",
        action="store_true",
        help="append the template keys the output lacks; never change an existing line",
    )
    args = parser.parse_args(argv)
    try:
        if args.add_missing and os.path.lexists(args.output):
            keys = update_env(args.template, args.output)
            if keys:
                print(f"appended {len(keys)} keys to {args.output}: {', '.join(keys)}")
            else:
                print(f"{args.output} already has every key of {args.template}; unchanged")
            return 0
        count = write_env(args.template, args.output)
    except FileExistsError:
        print(
            f"error: {args.output} already exists and was left unchanged. "
            "Use `make env-update` to add missing keys. Delete it first only if you "
            "want new secrets (this invalidates the database passwords of an existing "
            "volume; run `make db-reset` too).",
            file=sys.stderr,
        )
        return 1
    except EnvTemplateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.output} (mode 0600) with {count} generated secrets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
