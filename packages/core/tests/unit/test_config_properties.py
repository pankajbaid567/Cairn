"""Property test for cairn_core.config (design Property 5, Requirement 7.11).

Feature: cairn-phase-0-foundations, Property 5: Configuration errors name keys, not values

Each example builds a complete valid environment for one settings class, then
applies one to four defects (bad port, out-of-range number, unknown enum value,
malformed URL, malformed cron, missing or empty required secret). Every value
that is not a bare number carries a random hex marker, so the assertions look
for the marker and never for a short substring that could occur by chance.

Hypothesis does not reset function-scoped fixtures between examples, so this
module uses no fixtures: each example patches ``os.environ`` and changes into a
fresh temporary directory (no ``.env``) itself, then restores both.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from unittest.mock import patch

from hypothesis import given
from hypothesis import strategies as st

from cairn_core.config import (
    CONFIG_EXIT_STATUS,
    AdminSettings,
    ApiSettings,
    CommonSettings,
    WorkerSettings,
    env_keys,
    load_settings,
)

SETTINGS_CLASSES: tuple[type[CommonSettings], ...] = (
    CommonSettings,
    ApiSettings,
    WorkerSettings,
    AdminSettings,
)

ALL_ALIASES = frozenset(key for cls in SETTINGS_CLASSES for key in env_keys(cls))

REQUIRED_SECRETS: dict[type[CommonSettings], tuple[str, ...]] = {
    CommonSettings: (),
    ApiSettings: ("CAIRN_APP_DB_PASSWORD",),
    WorkerSettings: ("CAIRN_DBOS_DB_PASSWORD",),
    AdminSettings: (
        "CAIRN_OWNER_DB_PASSWORD",
        "CAIRN_APP_DB_PASSWORD",
        "CAIRN_DBOS_DB_PASSWORD",
    ),
}

# --- strategies ---------------------------------------------------------------------

# Environment values cannot hold NUL or lone surrogates.
_SURROGATES: tuple[Literal["Cs"], ...] = ("Cs",)
_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=_SURROGATES, blacklist_characters="\x00"),
    max_size=40,
)
# No whitespace of any kind, so a cron token never splits.
_WORD = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Z")), min_size=1, max_size=12
)
_MARKER = st.binary(min_size=8, max_size=8).map(lambda b: "mk" + b.hex())


@st.composite
def marked(draw: st.DrawFn) -> str:
    """Marker followed by arbitrary text. Never a valid number, enum member or URL."""
    return draw(_MARKER) + draw(_TEXT)


@st.composite
def secret_value(draw: st.DrawFn) -> str:
    return draw(_MARKER) + draw(_TEXT)


@st.composite
def bad_cron(draw: st.DrawFn) -> str:
    count = draw(st.sampled_from([1, 2, 3, 4, 7, 8, 9]))
    words = [draw(_WORD) for _ in range(count - 1)]
    return " ".join([draw(_MARKER), *words])


@st.composite
def good_cron(draw: st.DrawFn) -> str:
    count = draw(st.sampled_from([5, 6]))
    return " ".join(draw(_MARKER) if i == 0 else draw(_WORD) for i in range(count))


@st.composite
def huge_int_text(draw: st.DrawFn) -> str:
    """Out-of-range integers with at least seven digits, so they are distinctive."""
    value = draw(st.integers(min_value=10**6, max_value=10**15))
    return str(draw(st.sampled_from([1, -1])) * value)


@st.composite
def bad_timeout(draw: st.DrawFn) -> str:
    """Not a float, or a distinctive float outside (0, 30]."""
    kind = draw(st.sampled_from(["text", "big", "negative"]))
    if kind == "text":
        return draw(marked())
    number = draw(st.integers(min_value=10**6, max_value=10**12))
    return f"{number}.5" if kind == "big" else f"-{number}.5"


@dataclass(frozen=True)
class Defect:
    key: str
    value: str | None  # None: the variable is absent


def _defect_strategies(cls: type[CommonSettings]) -> list[st.SearchStrategy[Defect]]:
    def bad(key: str, values: st.SearchStrategy[str]) -> st.SearchStrategy[Defect]:
        return values.map(lambda v: Defect(key, v))

    strategies = [
        bad("CAIRN_ENV", marked()),
        bad("CAIRN_LOG_LEVEL", marked()),
        bad("CAIRN_DB_PORT", st.one_of(marked(), huge_int_text())),
        bad("CAIRN_DB_TIMEOUT_S", bad_timeout()),
        bad("CAIRN_DB_SSLMODE", marked()),
        bad("OTEL_TRACES_EXPORTER", marked()),
        bad("OTEL_EXPORTER_OTLP_ENDPOINT", marked()),
    ]
    if cls is WorkerSettings:
        strategies.append(bad("CAIRN_HEARTBEAT_CRON", bad_cron()))
    for key in REQUIRED_SECRETS[cls]:
        # An empty variable counts as unset, so both shapes must be reported as missing.
        strategies.append(st.builds(Defect, key=st.just(key), value=st.sampled_from([None, ""])))
    return strategies


@st.composite
def valid_environment(draw: st.DrawFn, cls: type[CommonSettings]) -> dict[str, str]:
    host = draw(_MARKER) + draw(_TEXT)
    env = {
        "CAIRN_ENV": draw(st.sampled_from(["local", "ci", "demo"])),
        "CAIRN_LOG_LEVEL": draw(st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR"])),
        "CAIRN_DB_HOST": host,
        "CAIRN_DB_PORT": str(draw(st.integers(min_value=1, max_value=65535))),
        "CAIRN_DB_NAME": draw(_MARKER),
        "CAIRN_DB_TIMEOUT_S": repr(
            draw(st.floats(min_value=0.001, max_value=30, allow_nan=False, allow_infinity=False))
        ),
        "OTEL_TRACES_EXPORTER": draw(st.sampled_from(["none", "otlp", "console"])),
    }
    if draw(st.booleans()):
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = (
            f"{draw(st.sampled_from(['http', 'https']))}://collector.example:"
            f"{draw(st.integers(min_value=1, max_value=65535))}"
        )
    if cls is WorkerSettings:
        env["CAIRN_HEARTBEAT_CRON"] = draw(good_cron())
    for key in REQUIRED_SECRETS[cls]:
        env[key] = draw(secret_value())
    if cls is AdminSettings and draw(st.booleans()):
        env["POSTGRES_PASSWORD"] = draw(secret_value())
    return env


# --- harness ------------------------------------------------------------------------


@contextlib.contextmanager
def isolated(env: dict[str, str]) -> Iterator[None]:
    """Only ``env`` is visible among the Cairn variables, and there is no ``.env``."""
    with patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        for alias in ALL_ALIASES:
            os.environ.pop(alias, None)
        os.environ.update(env)
        yield


def run_load(
    cls: type[CommonSettings], env: dict[str, str]
) -> tuple[CommonSettings | None, int | str | None, str, str]:
    """Return (settings, exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with isolated(env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            return load_settings(cls), None, out.getvalue(), err.getvalue()
        except SystemExit as exc:
            code = exc.code
            return (
                None,
                code if isinstance(code, (int, str)) else None,
                out.getvalue(),
                err.getvalue(),
            )


# --- properties ---------------------------------------------------------------------


@st.composite
def invalid_case(
    draw: st.DrawFn,
) -> tuple[type[CommonSettings], dict[str, str], dict[str, str | None]]:
    """A class, the environment to load it from, and the defects applied (key -> value)."""
    cls = draw(st.sampled_from(SETTINGS_CLASSES))
    env = draw(valid_environment(cls))
    defects = draw(
        st.lists(
            st.one_of(*_defect_strategies(cls)),
            min_size=1,
            max_size=4,
            unique_by=lambda d: d.key,
        )
    )
    applied: dict[str, str | None] = {}
    for defect in defects:
        applied[defect.key] = defect.value
        if defect.value is None:
            env.pop(defect.key, None)
        else:
            env[defect.key] = defect.value
    return cls, env, applied


@given(case=invalid_case())
def test_invalid_configuration_exits_2_naming_keys_and_never_values(
    case: tuple[type[CommonSettings], dict[str, str], dict[str, str | None]],
) -> None:
    """Feature: cairn-phase-0-foundations, Property 5: Configuration errors name keys, not values

    **Validates: Requirements 7.11**
    """
    cls, env, defects = case
    settings, code, stdout, stderr = run_load(cls, env)

    assert settings is None
    assert code == CONFIG_EXIT_STATUS
    assert stdout == ""
    assert stderr.startswith("invalid configuration: ")
    assert stderr.count("\n") == 1  # one line, however many keys failed
    for key in defects:
        assert key in stderr, f"{key} not named in {stderr!r}"
    # No key that was valid is reported: the offending set is exact.
    reported = {
        part.split(" (")[0] for part in stderr.removeprefix("invalid configuration: ").split(", ")
    }
    assert reported == set(defects)

    # No generated value leaks, whether it was the bad one or a valid one.
    # Short valid values such as "ci" or "INFO" are skipped: they could match by chance.
    for key, value in env.items():
        if value.startswith("mk"):
            assert value not in stderr, f"value of {key} leaked"
            assert value[:18] not in stderr, f"marker of {key} leaked"
    for key, bad_value in defects.items():
        if bad_value and (bad_value.startswith("mk") or len(bad_value) >= 7):
            assert bad_value not in stderr, f"bad value of {key} leaked"


@given(data=st.data())
def test_valid_configuration_never_exits(data: st.DataObject) -> None:
    """Feature: cairn-phase-0-foundations, Property 5: Configuration errors name keys, not values

    **Validates: Requirements 7.11**
    """
    cls = data.draw(st.sampled_from(SETTINGS_CLASSES))
    env = data.draw(valid_environment(cls))
    settings, code, stdout, stderr = run_load(cls, env)

    assert code is None
    assert isinstance(settings, cls)
    assert stdout == ""
    assert stderr == ""
