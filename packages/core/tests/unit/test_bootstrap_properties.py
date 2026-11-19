"""Property tests for cairn_core.bootstrap password hygiene (design Property 4).

Feature: cairn-phase-0-foundations, Property 4: Bootstrap never exposes plaintext passwords

Validates: Requirements 5.7, 5.8

Every password is printable ASCII (quotes, backslashes, ``$``, ``%``, braces and spaces
included) and carries a marker ``~mk-<16 hex>``. ``~`` is not a base64 character and does
not occur in any fixed SQL text, so the marker cannot appear in a verifier or a statement
by chance. Assertions look for the whole password and for the marker.

Everything runs without a database. ``bootstrap()`` runs against a small fake cluster that
answers the catalog queries and records each statement. The CLI runs through ``CliRunner``
with the connections replaced by that fake cluster.

The pure-Python SCRAM verifier (PBKDF2, 4096 iterations) is cheap enough, about 2 ms with
OpenSSL, to run in every example, so no property lowers ``max_examples``. ``deadline`` is
off because a run does up to nine derivations and the laptop may be busy.

Hypothesis does not reset function-scoped fixtures between examples, so this module uses
none: each example patches the environment, the working directory and the logging
configuration itself and restores them.
"""

from __future__ import annotations

import base64
import logging
import re
import tempfile
import traceback
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import psycopg
import pytest
from click.testing import CliRunner
from click.testing import Result as CliResult
from hypothesis import given, settings
from hypothesis import strategies as st
from psycopg import sql

from cairn_core import cli
from cairn_core.bootstrap import (
    APP_ROLE,
    DBOS_ROLE,
    MANAGED_ROLES,
    OWNER_ROLE,
    BootstrapError,
    BootstrapResult,
    ScramEncryptor,
    bootstrap,
    describe,
    is_scram_verifier,
    role_statement,
    role_statements,
    scram_sha256_verifier,
)
from cairn_core.config import AdminSettings


@pytest.fixture(autouse=True)
def _hermetic_no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The repo-root .env holds CAIRN_INGEST_DB_PASSWORD; these tests expect the Phase 0
    three-role bootstrap, so run them where no .env is found."""
    monkeypatch.delenv("CAIRN_INGEST_DB_PASSWORD", raising=False)
    monkeypatch.delenv("CAIRN_EDGE_DB_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)


Mode = Literal["libpq", "python", "libpq_broken"]
PGCONN_MODES: tuple[Mode, ...] = ("libpq", "python", "libpq_broken")

# Literal copy of the required attributes (requirement 5.1), not imported, so a change in
# the module under test cannot change the expectation.
ATTRIBUTES = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
MARKER_RE = re.compile(r"~mk-[0-9a-f]{16}")
VERIFIER_IN_SQL_RE = re.compile(r"'SCRAM-SHA-256\$[^']*'")

PBT = settings(deadline=None)

# --- secrets and strategies --------------------------------------------------------------


def marker_of(password: str) -> str:
    found = MARKER_RE.search(password)
    assert found is not None, "every generated password carries a marker"
    return found.group(0)


@dataclass(frozen=True)
class Secrets:
    """The three role passwords and the superuser password of one example."""

    owner: str
    app: str
    dbos: str
    superuser: str

    @property
    def by_role(self) -> dict[str, str]:
        return {OWNER_ROLE: self.owner, APP_ROLE: self.app, DBOS_ROLE: self.dbos}

    @property
    def everything(self) -> tuple[str, ...]:
        return (self.owner, self.app, self.dbos, self.superuser)

    def leaks(self, blob: str) -> list[str]:
        return [s for s in self.everything if s in blob or marker_of(s) in blob]

    def with_non_ascii(self, role: str, extra: str) -> Secrets:
        by_role = {**self.by_role, role: extra + self.by_role[role]}
        return Secrets(by_role[OWNER_ROLE], by_role[APP_ROLE], by_role[DBOS_ROLE], self.superuser)

    def settings(self) -> AdminSettings:
        return AdminSettings.model_validate(
            {
                "POSTGRES_PASSWORD": self.superuser,
                "CAIRN_OWNER_DB_PASSWORD": self.owner,
                "CAIRN_APP_DB_PASSWORD": self.app,
                "CAIRN_DBOS_DB_PASSWORD": self.dbos,
            }
        )

    def env(self) -> dict[str, str]:
        return {
            "POSTGRES_PASSWORD": self.superuser,
            "CAIRN_OWNER_DB_PASSWORD": self.owner,
            "CAIRN_APP_DB_PASSWORD": self.app,
            "CAIRN_DBOS_DB_PASSWORD": self.dbos,
        }


_ASCII = st.characters(min_codepoint=32, max_codepoint=126)
_NASTY = st.sampled_from(
    ["'", "''", '"', "\\", "\\'", "$", "$$", "$1", "%", "%s", "%(x)s", "{}", "{0}", " ", ";", "--"]
)
_MARKER = st.binary(min_size=8, max_size=8).map(lambda b: "~mk-" + b.hex())
_NON_ASCII = st.text(
    alphabet=st.characters(min_codepoint=128, whitelist_categories=("L", "N", "S")),
    min_size=1,
    max_size=3,
)


@st.composite
def passwords(draw: st.DrawFn) -> str:
    """Printable ASCII, at least 12 characters, with the marker somewhere inside."""
    pieces = draw(st.lists(st.one_of(st.text(_ASCII, max_size=8), _NASTY), max_size=5))
    pieces.append(draw(_MARKER))
    return "".join(draw(st.permutations(pieces)))


@st.composite
def secret_sets(draw: st.DrawFn) -> Secrets:
    four = draw(st.lists(passwords(), min_size=4, max_size=4, unique_by=marker_of))
    return Secrets(four[0], four[1], four[2], four[3])


# --- helpers: rendering, verifiers, logging ------------------------------------------------


def render(statement: sql.Composable) -> str:
    return statement.as_string()


def fake_encrypt(password: str, role: str) -> str:
    """Stands in for any encryptor: a well-formed verifier that does not contain the password."""
    salt = base64.b64encode(f"salt-{role}".encode()).decode()
    return f"SCRAM-SHA-256$4096:{salt}$c3RvcmVkLWtleQ==:c2VydmVyLWtleQ=="


def verifier_matches(verifier: str, password: str) -> bool:
    """True if ``verifier`` is the SCRAM verifier of ``password`` for its own salt."""
    found = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([^$]+)\$.*", verifier)
    assert found is not None
    rebuilt = scram_sha256_verifier(
        password, salt=base64.b64decode(found.group(2)), iterations=int(found.group(1))
    )
    return rebuilt == verifier


def check_role_statement(text: str, role: str, *, exists: bool) -> str:
    """Exact attributes and a SCRAM verifier in the password position. Returns the verifier."""
    verb = "ALTER" if exists else "CREATE"
    inherit = "NOINHERIT" if role == APP_ROLE else "INHERIT"
    prefix = f'{verb} ROLE "{role}" WITH {ATTRIBUTES} {inherit} PASSWORD '
    assert text.startswith(prefix), text
    assert "PASSWORD 'SCRAM-SHA-256$" in text
    found = re.fullmatch(r"'(SCRAM-SHA-256\$[^']*)'", text[len(prefix) :])
    assert found is not None, text
    assert is_scram_verifier(found.group(1))
    return found.group(1)


class Capture(logging.Handler):
    """Collects every record as text: message, all fields, formatted exception."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.blobs: list[str] = []
        self._formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        self.blobs.append(record.getMessage())
        self.blobs.append(repr(record.__dict__))
        self.blobs.append(self._formatter.format(record))


@contextmanager
def capture_logs() -> Iterator[Capture]:
    """Capture all logging, then restore the root logger (the CLI reconfigures it)."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    capture = Capture()
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def assert_clean(secrets: Secrets, blobs: Iterable[str]) -> None:
    for blob in blobs:
        assert secrets.leaks(blob) == [], blob


def exception_blobs(exc: BaseException) -> list[str]:
    return [str(exc), repr(exc), "".join(traceback.format_exception(exc))]


# --- a fake cluster ----------------------------------------------------------------------


class Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class LibpqLike:
    """A ``pgconn`` with ``encrypt_password``. The broken one puts the password in its error."""

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken

    def encrypt_password(self, passwd: bytes, user: bytes, algorithm: bytes | None = None) -> bytes:
        if self.broken:
            raise psycopg.OperationalError(f"password encryption failed for {passwd!r}")
        return scram_sha256_verifier(passwd.decode()).encode()


class StubConn:
    def __init__(self, pgconn: object) -> None:
        self.pgconn = pgconn


def pgconn_for(mode: Mode) -> object | None:
    return {
        "libpq": LibpqLike(),
        "python": None,
        "libpq_broken": LibpqLike(broken=True),
    }[mode]


class Cluster:
    """Catalog state shared by every session; records every write that was accepted."""

    def __init__(self) -> None:
        self.roles: set[str] = set()
        self.memberships: list[tuple[str, str]] = []
        self.databases: dict[str, str] = {}
        self.extensions: dict[str, list[str]] = {}
        self.log: list[str] = []
        self.admin_row: tuple[Any, ...] = ("postgres", True, True, True, True)
        self.fail: Callable[[str], Exception | None] = lambda _text: None

    def session(self, dbname: str = "postgres", *, pgconn: object | None = None) -> Session:
        return Session(self, dbname, pgconn)


class Session:
    def __init__(self, cluster: Cluster, dbname: str, pgconn: object | None) -> None:
        self.cluster = cluster
        self.dbname = dbname
        self.autocommit = True
        self.pgconn = pgconn

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: object, params: tuple[Any, ...] | None = None) -> Result:
        c = self.cluster
        text = query.as_string() if isinstance(query, sql.Composable) else str(query)
        if "session_user" in text:
            return Result([c.admin_row])
        if "FROM pg_roles WHERE rolname" in text:
            assert params is not None
            return Result([(r,) for r in sorted(c.roles) if r in params[0]])
        if "pg_auth_members" in text:
            assert params is not None
            return Result([(m, g) for m, g in c.memberships if m in params[0]])
        if "FROM pg_database" in text:
            assert params is not None
            owner = c.databases.get(params[0])
            return Result([] if owner is None else [(owner,)])
        error = c.fail(text)
        if error is not None:
            raise error
        c.log.append(f"[{self.dbname}] {text}")
        if m := re.match(r'CREATE ROLE "(\w+)"', text):
            c.roles.add(m.group(1))
        elif m := re.match(r'(?:CREATE|ALTER) DATABASE "(\w+)" OWNER (?:TO )?"(\w+)"', text):
            c.databases[m.group(1)] = m.group(2)
        elif m := re.match(r'CREATE EXTENSION IF NOT EXISTS "(\w+)"', text):
            installed = c.extensions.setdefault(self.dbname, [])
            if m.group(1) not in installed:
                installed.append(m.group(1))
        return Result([])


def run_bootstrap(
    cluster: Cluster, secrets: Secrets, mode: Mode | Literal["custom"]
) -> BootstrapResult:
    """``bootstrap()`` against the fake cluster. ``custom`` passes an explicit encryptor."""
    admin = cluster.session(pgconn=None if mode == "custom" else pgconn_for(mode))
    return bootstrap(
        cast("psycopg.Connection[Any]", admin),
        secrets.settings(),
        connect=lambda name: cast("psycopg.Connection[Any]", cluster.session(name)),
        encrypt=fake_encrypt if mode == "custom" else None,
    )


# --- a CLI harness -----------------------------------------------------------------------


@dataclass
class CliHarness:
    cluster: Cluster
    events: list[str]
    runner: CliRunner

    def invoke(self, command: str) -> CliResult:
        return self.runner.invoke(cli.main, [command])


@contextmanager
def cli_harness(secrets: Secrets, cluster: Cluster, mode: Mode) -> Iterator[CliHarness]:
    """Run ``cairn-db`` with the connections and migrations replaced.

    Sets the four passwords in the environment, clears every other settings key, works in a
    fresh directory (no ``.env``) and undoes all of it afterwards.
    """
    events: list[str] = []

    def connect_as(
        _: AdminSettings, *, user: str, password: str, dbname: str | None = None
    ) -> Session:
        events.append(f"connect:{user}")
        return cluster.session(dbname or "cairn", pgconn=pgconn_for(mode))

    def run(
        conn: object, settings_: AdminSettings, *, allow_plaintext: bool = False
    ) -> BootstrapResult:
        events.append("bootstrap")
        return bootstrap(
            cast("psycopg.Connection[Any]", conn),
            settings_,
            allow_plaintext=allow_plaintext,
            connect=lambda name: cast("psycopg.Connection[Any]", cluster.session(name)),
        )

    def fake_migrate(_conn: object, migrations: Any) -> list[int]:
        events.append("migrate")
        return [m.version for m in migrations]

    aliases = {f.alias for f in AdminSettings.model_fields.values() if f.alias is not None}
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        for alias in aliases:
            mp.delenv(alias, raising=False)
        mp.chdir(tmp)
        for key, value in secrets.env().items():
            mp.setenv(key, value)
        mp.setattr(cli, "connect_as", connect_as)
        mp.setattr(cli, "run_bootstrap", run)
        mp.setattr(cli, "migrate", fake_migrate)
        yield CliHarness(cluster, events, CliRunner())


def cli_blobs(result: CliResult, harness: CliHarness, *, chained: bool = True) -> list[str]:
    """What the command showed. ``chained=False`` skips the traceback chain, for a hostile
    server error that is chained on purpose and never printed."""
    blobs = [result.output, repr(result.exception), *harness.cluster.log]
    if chained and result.exc_info is not None and result.exc_info[1] is not None:
        blobs.extend(exception_blobs(result.exc_info[1]))
    return blobs


# --- statements built from generated passwords -------------------------------------------

EncryptorKind = Literal["fake", "python", "libpq", "libpq_broken"]


def encryptor_for(kind: EncryptorKind) -> Callable[[str, str], str]:
    if kind == "fake":
        return fake_encrypt
    if kind == "python":
        return ScramEncryptor(None)
    return ScramEncryptor(StubConn(pgconn_for(kind)))


@PBT
@given(
    secrets=secret_sets(),
    kind=st.sampled_from(["fake", "python", "libpq", "libpq_broken"]),
    existing=st.sets(st.sampled_from(MANAGED_ROLES)),
)
def test_role_statements_hold_only_verifiers_and_exact_attributes(
    secrets: Secrets, kind: EncryptorKind, existing: set[str]
) -> None:
    """Property 4: Bootstrap never exposes plaintext passwords."""
    statements = role_statements(secrets.by_role, existing, encryptor_for(kind))
    assert len(statements) == 3
    for role, statement in zip(MANAGED_ROLES, statements, strict=True):
        text = render(statement)
        assert secrets.leaks(text) == [], text
        verifier = check_role_statement(text, role, exists=role in existing)
        if kind != "fake":
            assert verifier_matches(verifier, secrets.by_role[role])


@PBT
@given(
    role=st.sampled_from(MANAGED_ROLES),
    exists=st.booleans(),
    marker=_MARKER,
    free=st.text(max_size=30),
    variant=st.sampled_from(["free", "after_valid", "before_valid", "injection", "md5"]),
)
def test_a_value_that_is_not_a_verifier_is_refused_without_echo(
    role: str, exists: bool, marker: str, free: str, variant: str
) -> None:
    """Property 4: Bootstrap never exposes plaintext passwords."""
    good = fake_encrypt("x", role)
    value = {
        "free": free + marker,
        "after_valid": good + marker,
        "before_valid": marker + good,
        "injection": f"{good}'; DROP ROLE postgres; --{marker}",
        "md5": "md5" + "0123456789abcdef" * 2 + marker,
    }[variant]
    assert not is_scram_verifier(value)
    with pytest.raises(BootstrapError) as err:
        role_statement(role, value, exists=exists)
    for blob in exception_blobs(err.value):
        assert value not in blob
        assert marker not in blob
    assert role in str(err.value)


@PBT
@given(
    secrets=secret_sets(),
    role=st.sampled_from(MANAGED_ROLES),
    extra=_NON_ASCII,
    kind=st.sampled_from(["python", "libpq_broken"]),
)
def test_a_non_ascii_password_is_refused_without_echo(
    secrets: Secrets, role: str, extra: str, kind: EncryptorKind
) -> None:
    """Property 4: Bootstrap never exposes plaintext passwords."""
    bad = secrets.with_non_ascii(role, extra)
    password = bad.by_role[role]
    with pytest.raises(BootstrapError) as direct:
        scram_sha256_verifier(password)
    with pytest.raises(BootstrapError) as built:
        role_statements(bad.by_role, (), encryptor_for(kind))
    for exc in (direct.value, built.value):
        for blob in exception_blobs(exc):
            assert marker_of(password) not in blob
            assert password not in blob
            assert extra not in blob


# --- bootstrap() on the fake cluster -----------------------------------------------------


@PBT
@given(secrets=secret_sets(), mode=st.sampled_from([*PGCONN_MODES, "custom"]))
def test_bootstrap_exposes_no_password_anywhere(
    secrets: Secrets, mode: Mode | Literal["custom"]
) -> None:
    """Property 4: Bootstrap never exposes plaintext passwords."""
    cluster = Cluster()
    with capture_logs() as cap:
        result = run_bootstrap(cluster, secrets, mode)
    blobs = [
        *cluster.log,
        repr(result),
        repr(result.roles),
        repr(result.databases),
        *describe(result),
        *cap.blobs,
    ]
    assert_clean(secrets, blobs)
    # The capture works: bootstrap logs role and database names.
    assert "bootstrap.role" in " ".join(cap.blobs)
    assert "bootstrap.database" in " ".join(cap.blobs)
    # Every role statement carries a verifier, and (outside the fake) the right one.
    role_lines = {
        m.group(1): line for line in cluster.log if (m := re.search(r'ROLE "(\w+)"', line))
    }
    assert set(role_lines) == set(MANAGED_ROLES)
    for role, line in role_lines.items():
        text = line.split("] ", 1)[1]
        verifier = check_role_statement(text, role, exists=False)
        if mode != "custom":
            assert verifier_matches(verifier, secrets.by_role[role])


@PBT
@given(secrets=secret_sets(), mode=st.sampled_from(["libpq", "python"]))
def test_running_bootstrap_again_repeats_the_same_statements(secrets: Secrets, mode: Mode) -> None:
    """Property 4: Bootstrap never exposes plaintext passwords."""

    def run_and_slice(cluster: Cluster) -> list[str]:
        start = len(cluster.log)
        run_bootstrap(cluster, secrets, mode)
        return cluster.log[start:]

    def normalized(lines: Iterable[str]) -> list[str]:
        return [VERIFIER_IN_SQL_RE.sub("'<verifier>'", line) for line in lines]

    def verifiers(lines: Iterable[str]) -> list[str]:
        return [m.group(0) for line in lines for m in VERIFIER_IN_SQL_RE.finditer(line)]

    cluster = Cluster()
    first, second, third = (run_and_slice(cluster) for _ in range(3))
    assert sum("CREATE DATABASE" in line for line in first) == 2
    assert not any("CREATE DATABASE" in line or "CREATE ROLE" in line for line in second)
    assert normalized(second) == normalized(third)
    created_then_altered = [
        line.replace("CREATE ROLE", "ALTER ROLE")
        for line in normalized(first)
        if "CREATE DATABASE" not in line
    ]
    assert created_then_altered == normalized(second)
    # Only the verifier text differs between runs (fresh salt each time).
    assert len(verifiers(second)) == len(verifiers(third)) == 3
    assert set(verifiers(second)).isdisjoint(verifiers(third))
    assert_clean(secrets, [*first, *second, *third])


# --- failures ----------------------------------------------------------------------------

FailureKind = Literal["statement", "membership", "non_ascii"]
STATEMENT_KEYWORDS = ("ROLE", "CREATE DATABASE", "CREATE EXTENSION", "GRANT CONNECT")


@dataclass(frozen=True)
class FailureCase:
    secrets: Secrets
    kind: FailureKind
    keyword: str
    member: str
    granted: str
    non_ascii_role: str
    extra: str
    mode: Mode

    def prepare(self) -> Cluster:
        """A cluster in which bootstrap must fail in the way ``kind`` says."""
        cluster = Cluster()
        if self.kind == "statement":
            keyword = self.keyword

            def fail(text: str) -> Exception | None:
                if keyword in text:
                    # A hostile server message: it echoes every secret it can think of.
                    return psycopg.errors.InsufficientPrivilege(
                        "permission denied; " + " ".join(self.secrets.everything)
                    )
                return None

            cluster.fail = fail
        elif self.kind == "membership":
            cluster.roles = set(MANAGED_ROLES)
            cluster.memberships = [(self.member, self.granted)]
        return cluster

    @property
    def effective_secrets(self) -> Secrets:
        if self.kind == "non_ascii":
            return self.secrets.with_non_ascii(self.non_ascii_role, self.extra)
        return self.secrets


@st.composite
def failure_cases(draw: st.DrawFn) -> FailureCase:
    kind: FailureKind = draw(st.sampled_from(["statement", "membership", "non_ascii"]))
    return FailureCase(
        secrets=draw(secret_sets()),
        kind=kind,
        keyword=draw(st.sampled_from(STATEMENT_KEYWORDS)),
        member=draw(st.sampled_from([APP_ROLE, DBOS_ROLE])),
        granted=draw(st.sampled_from(["pg_monitor", "cairn_owner", "pg_read_all_data"])),
        non_ascii_role=draw(st.sampled_from(MANAGED_ROLES)),
        extra=draw(_NON_ASCII),
        mode=draw(
            st.sampled_from(["python", "libpq_broken"] if kind == "non_ascii" else PGCONN_MODES)
        ),
    )


@PBT
@given(case=failure_cases())
def test_bootstrap_failures_do_not_leak_a_password(case: FailureCase) -> None:
    """Property 4: Bootstrap never exposes plaintext passwords."""
    secrets = case.effective_secrets
    cluster = case.prepare()
    with (
        capture_logs() as cap,
        pytest.raises((psycopg.errors.InsufficientPrivilege, BootstrapError)) as err,
    ):
        run_bootstrap(cluster, secrets, case.mode)
    exc = err.value
    if case.kind == "statement":
        assert isinstance(exc, psycopg.errors.InsufficientPrivilege)
        # The hostile message is the server's own; bootstrap adds nothing of its own.
        blobs = [*cluster.log, *cap.blobs]
    else:
        assert isinstance(exc, BootstrapError)
        blobs = [*exception_blobs(exc), *cluster.log, *cap.blobs]
        assert cluster.log == [], "refused before any write"
        if case.kind == "membership":
            assert f"{case.member} is a member of {case.granted}" in str(exc)
    assert_clean(secrets, blobs)


@PBT
@given(case=failure_cases(), command=st.sampled_from(["bootstrap", "init"]))
def test_cli_failures_do_not_leak_a_password(case: FailureCase, command: str) -> None:
    """Property 4: Bootstrap never exposes plaintext passwords."""
    secrets = case.effective_secrets
    cluster = case.prepare()
    with capture_logs() as cap, cli_harness(secrets, cluster, case.mode) as harness:
        result = harness.invoke(command)
    assert result.exit_code == 1, result.output
    assert "migrate" not in harness.events
    if case.kind == "statement":
        assert "database error: InsufficientPrivilege" in result.output
    else:
        assert "bootstrap failed: " in result.output
        assert cluster.log == []
    blobs = cli_blobs(result, harness, chained=case.kind != "statement")
    assert_clean(secrets, [*blobs, *cap.blobs])


# --- cairn-db bootstrap and init on success ----------------------------------------------


@PBT
@given(
    secrets=secret_sets(),
    mode=st.sampled_from(PGCONN_MODES),
    command=st.sampled_from(["bootstrap", "init"]),
)
def test_cli_output_holds_no_password(secrets: Secrets, mode: Mode, command: str) -> None:
    """Property 4: Bootstrap never exposes plaintext passwords."""
    cluster = Cluster()
    with capture_logs() as cap, cli_harness(secrets, cluster, mode) as harness:
        result = harness.invoke(command)
    assert result.exit_code == 0, result.output
    assert "bootstrap complete (password encryption: " in result.output
    assert ("migrate" in harness.events) == (command == "init")
    assert_clean(secrets, [*cli_blobs(result, harness), *cap.blobs])
