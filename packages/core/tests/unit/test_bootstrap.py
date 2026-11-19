"""Unit tests for cairn_core.bootstrap and the cairn-db bootstrap/init commands.

No database is needed. The statement builders are pure. ``bootstrap()`` runs against a
small fake cluster that answers the catalog queries bootstrap makes and records every
statement it executes. Behavior against a real Postgres (roles really created, the
second run changing nothing in the catalog) is covered by the integration tests in
task 4.2.
"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from click.testing import CliRunner
from psycopg import pq, sql

from cairn_core import cli
from cairn_core.bootstrap import (
    APP_ROLE,
    DBOS_ROLE,
    MIN_EXTENSION_VERSIONS,
    OWNER_ROLE,
    REQUIRED_EXTENSIONS,
    AdminFacts,
    BootstrapError,
    BootstrapResult,
    DatabaseResult,
    DatabaseSpec,
    EmulationResult,
    PlaintextRefused,
    RoleResult,
    RoleRow,
    ScramEncryptor,
    admin_access_revoke_statements,
    admin_set_grants,
    bootstrap,
    bootstrap_database,
    content_spec,
    database_statements,
    dbos_spec,
    describe,
    describe_emulation,
    emulate_managed_admin,
    emulate_managed_admin_statements,
    emulation_refusals,
    extension_statements,
    is_scram_verifier,
    membership_violations,
    password_form,
    plaintext_flag_problems,
    read_admin_facts,
    resolve_mode,
    role_attribute_violations,
    role_statement,
    role_statements,
    scram_sha256_verifier,
    tls_parameters,
)
from cairn_core.config import AdminSettings, EmulationSettings, env_keys
from cairn_core.migrate import bundled_migrations


@pytest.fixture(autouse=True)
def _hermetic_no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The repo-root .env holds CAIRN_INGEST_DB_PASSWORD; these tests expect the Phase 0
    three-role bootstrap, so run them where no .env is found."""
    monkeypatch.delenv("CAIRN_INGEST_DB_PASSWORD", raising=False)
    monkeypatch.delenv("CAIRN_EDGE_DB_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)


OWNER_PW = "owner-pw-FAKE-Zq81xV0aLm34"
APP_PW = "app-pw-FAKE-Rt55yU9cKd72"
DBOS_PW = "dbos-pw-FAKE-Hn26wE3sBf90"
SUPER_PW = "super-pw-FAKE-Jp47tG1dNc68"
PASSWORDS = {OWNER_ROLE: OWNER_PW, APP_ROLE: APP_PW, DBOS_ROLE: DBOS_PW}
ALL_SECRETS = (OWNER_PW, APP_PW, DBOS_PW, SUPER_PW)
GOOD_VERIFIER = "SCRAM-SHA-256$4096:c2FsdHNhbHRzYWx0c2E=$c3RvcmVka2V5Cg==:c2VydmVya2V5Cg=="


def render(statement: sql.Composable) -> str:
    return statement.as_string()


def fake_encrypt(password: str, role: str) -> str:
    """Stands in for libpq: a well-formed verifier that does not contain the password."""
    salt = base64.b64encode(f"salt-{role}".encode()).decode()
    return f"SCRAM-SHA-256$4096:{salt}$c3RvcmVkLWtleQ==:c2VydmVyLWtleQ=="


# --- verifiers ---------------------------------------------------------------------------


def test_python_verifier_has_postgres_format() -> None:
    v = scram_sha256_verifier("correct horse", salt=b"0123456789abcdef")
    assert is_scram_verifier(v)
    assert v.startswith("SCRAM-SHA-256$4096:MDEyMzQ1Njc4OWFiY2RlZg==$")
    assert "correct horse" not in v


def test_python_verifier_uses_a_fresh_salt_each_time() -> None:
    assert scram_sha256_verifier("same") != scram_sha256_verifier("same")


def test_python_verifier_rejects_non_ascii_without_echoing_the_password() -> None:
    with pytest.raises(BootstrapError) as err:
        scram_sha256_verifier("pässword-secret")
    assert "pässword" not in str(err.value)


@pytest.mark.parametrize(
    "value",
    ["", "hunter2", "md5" + "0" * 32, "SCRAM-SHA-256", "SCRAM-SHA-256$x:y$z:w", OWNER_PW],
)
def test_is_scram_verifier_rejects_everything_else(value: str) -> None:
    assert not is_scram_verifier(value)


def test_python_verifier_matches_libpq_for_the_same_salt_and_iterations() -> None:
    """Cross-check against the libpq in psycopg. Explicit algorithm needs no server."""
    conn = pq.PGconn.connect(b"host=/nonexistent-cairn-socket dbname=x connect_timeout=1")
    libpq = conn.encrypt_password(b"pw-for-cross-check", b"someone", b"scram-sha-256").decode()
    conn.finish()
    iterations, salt = re.match(r"SCRAM-SHA-256\$(\d+):([^$]+)\$", libpq).groups()  # type: ignore[union-attr]
    mine = scram_sha256_verifier(
        "pw-for-cross-check", salt=base64.b64decode(salt), iterations=int(iterations)
    )
    assert mine == libpq


class StubPg:
    """A ``pgconn`` whose encrypt_password records its arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, bytes, bytes | None]] = []

    def encrypt_password(self, passwd: bytes, user: bytes, algorithm: bytes | None = None) -> bytes:
        self.calls.append((passwd, user, algorithm))
        return fake_encrypt(passwd.decode(), user.decode()).encode()


class StubConn:
    def __init__(self, pgconn: object) -> None:
        self.pgconn = pgconn


def test_encryptor_prefers_libpq_with_an_explicit_algorithm() -> None:
    pg = StubPg()
    enc = ScramEncryptor(StubConn(pg))
    verifier = enc(APP_PW, APP_ROLE)
    assert pg.calls == [(APP_PW.encode(), APP_ROLE.encode(), b"scram-sha-256")]
    assert enc.method == "libpq"
    assert is_scram_verifier(verifier)


def test_encryptor_falls_back_to_python_when_libpq_is_unavailable() -> None:
    for conn in (None, StubConn(None), object()):
        enc = ScramEncryptor(conn)
        assert is_scram_verifier(enc(APP_PW, APP_ROLE))
        assert enc.method == "python"


def test_encryptor_falls_back_when_libpq_raises() -> None:
    class Broken:
        def encrypt_password(self, *_: Any) -> bytes:
            raise psycopg.OperationalError("password encryption failed")

    enc = ScramEncryptor(StubConn(Broken()))
    assert is_scram_verifier(enc(APP_PW, APP_ROLE))
    assert enc.method == "python"


# --- role statements ---------------------------------------------------------------------


def test_create_role_statement_has_the_exact_attributes() -> None:
    text = render(role_statement(OWNER_ROLE, GOOD_VERIFIER, exists=False))
    assert text == (
        'CREATE ROLE "cairn_owner" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
        f"NOREPLICATION NOBYPASSRLS INHERIT PASSWORD '{GOOD_VERIFIER}'"
    )


def test_existing_roles_are_altered_not_created() -> None:
    text = render(role_statement(DBOS_ROLE, GOOD_VERIFIER, exists=True))
    assert text.startswith('ALTER ROLE "cairn_dbos" WITH LOGIN NOSUPERUSER')


def test_only_the_app_role_is_noinherit() -> None:
    for role in (OWNER_ROLE, DBOS_ROLE):
        assert " INHERIT PASSWORD" in render(role_statement(role, GOOD_VERIFIER, exists=False))
        assert "NOINHERIT" not in render(role_statement(role, GOOD_VERIFIER, exists=False))
    assert " NOINHERIT PASSWORD" in render(role_statement(APP_ROLE, GOOD_VERIFIER, exists=False))


@pytest.mark.parametrize(
    "attribute", ["SUPERUSER", "BYPASSRLS", "CREATEROLE", "CREATEDB", "REPLICATION"]
)
def test_no_role_gains_a_dangerous_attribute(attribute: str) -> None:
    for role in (OWNER_ROLE, APP_ROLE, DBOS_ROLE):
        text = render(role_statement(role, GOOD_VERIFIER, exists=True))
        # Present only in its NO… form.
        assert f"NO{attribute}" in text
        assert not re.search(rf"(?<!NO){attribute}", text)


def test_role_statement_refuses_a_plaintext_password_without_echoing_it() -> None:
    with pytest.raises(BootstrapError) as err:
        role_statement(APP_ROLE, APP_PW, exists=False)
    assert APP_PW not in str(err.value)
    assert APP_ROLE in str(err.value)


def test_role_statement_quotes_identifiers() -> None:
    with pytest.raises(BootstrapError):
        role_statement('x"; DROP ROLE y; --', GOOD_VERIFIER, exists=False)


def test_role_statements_cover_every_role_in_order_and_hold_no_plaintext() -> None:
    statements = role_statements(PASSWORDS, existing={APP_ROLE}, encrypt=fake_encrypt)
    texts = [render(s) for s in statements]
    assert [t.split()[0] for t in texts] == ["CREATE", "ALTER", "CREATE"]
    assert [re.search(r'ROLE "(\w+)"', t).group(1) for t in texts] == [  # type: ignore[union-attr]
        OWNER_ROLE,
        APP_ROLE,
        DBOS_ROLE,
    ]
    for text in texts:
        assert not any(secret in text for secret in ALL_SECRETS)
        assert "PASSWORD 'SCRAM-SHA-256$" in text


def test_role_statements_reject_an_encryptor_that_returns_plaintext() -> None:
    with pytest.raises(BootstrapError) as err:
        role_statements(PASSWORDS, existing=(), encrypt=lambda password, role: password)
    assert not any(secret in str(err.value) for secret in ALL_SECRETS)


def test_role_statements_need_a_password_for_every_role() -> None:
    with pytest.raises(BootstrapError, match=DBOS_ROLE):
        role_statements({OWNER_ROLE: OWNER_PW, APP_ROLE: APP_PW}, (), fake_encrypt)
    with pytest.raises(BootstrapError, match=OWNER_ROLE):
        role_statements({**PASSWORDS, OWNER_ROLE: ""}, (), fake_encrypt)


# --- memberships -------------------------------------------------------------------------


def test_membership_violations_name_each_membership() -> None:
    rows = [(APP_ROLE, "pg_monitor"), (DBOS_ROLE, OWNER_ROLE), (OWNER_ROLE, "pg_read_all_data")]
    assert membership_violations(rows) == [
        "cairn_app is a member of pg_monitor",
        "cairn_dbos is a member of cairn_owner",
    ]
    assert membership_violations([]) == []


# --- database statements -----------------------------------------------------------------


def test_new_content_database_statements() -> None:
    texts = [render(s) for s in database_statements(content_spec(), current_owner=None)]
    assert texts == [
        'CREATE DATABASE "cairn" OWNER "cairn_owner"',
        'REVOKE CONNECT, TEMPORARY ON DATABASE "cairn" FROM PUBLIC',
        'REVOKE CONNECT ON DATABASE "cairn" FROM "cairn_dbos"',
        'GRANT CONNECT ON DATABASE "cairn" TO "cairn_owner"',
        'GRANT CONNECT ON DATABASE "cairn" TO "cairn_app"',
    ]


def test_dbos_database_is_open_to_dbos_only() -> None:
    texts = [render(s) for s in database_statements(dbos_spec(), current_owner=None)]
    assert texts == [
        'CREATE DATABASE "cairn_dbos" OWNER "cairn_dbos"',
        'REVOKE CONNECT, TEMPORARY ON DATABASE "cairn_dbos" FROM PUBLIC',
        'REVOKE CONNECT ON DATABASE "cairn_dbos" FROM "cairn_owner"',
        'REVOKE CONNECT ON DATABASE "cairn_dbos" FROM "cairn_app"',
        'GRANT CONNECT ON DATABASE "cairn_dbos" TO "cairn_dbos"',
    ]


def test_existing_database_is_not_recreated_and_a_wrong_owner_is_fixed() -> None:
    same = [render(s) for s in database_statements(content_spec(), current_owner=OWNER_ROLE)]
    assert not any("CREATE DATABASE" in t or "ALTER DATABASE" in t for t in same)
    moved = [render(s) for s in database_statements(content_spec(), current_owner="postgres")]
    assert moved[0] == 'ALTER DATABASE "cairn" OWNER TO "cairn_owner"'


def test_database_names_are_validated() -> None:
    with pytest.raises(BootstrapError):
        database_statements(content_spec('a"b'), current_owner=None)
    with pytest.raises(BootstrapError):
        database_statements(content_spec("Cairn"), current_owner=None)


def test_required_extensions_are_vector_and_btree_gist() -> None:
    assert REQUIRED_EXTENSIONS == ("vector", "btree_gist")
    assert MIN_EXTENSION_VERSIONS == {"vector": (0, 8, 0)}
    assert content_spec().extensions == REQUIRED_EXTENSIONS


def test_extension_statements() -> None:
    texts = [render(s) for s in extension_statements(content_spec())]
    assert texts == [
        'CREATE EXTENSION IF NOT EXISTS "vector"',
        'CREATE EXTENSION IF NOT EXISTS "btree_gist"',
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
    ]
    assert [render(s) for s in extension_statements(dbos_spec())] == [
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC"
    ]


# --- bootstrap() against a fake cluster --------------------------------------------------


class Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class Cluster:
    """Catalog state shared by every session; records every write in order."""

    def __init__(self) -> None:
        self.roles: set[str] = set()
        self.memberships: list[tuple[str, str]] = []
        self.databases: dict[str, str] = {}
        self.extensions: dict[str, list[str]] = {}
        self.log: list[str] = []
        # session_user, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls
        self.admin_row: tuple[Any, ...] = ("postgres", True, True, True, True)
        # rolsuper, rolreplication, rolbypassrls of roles that exist (default: all false)
        self.role_attrs: dict[str, tuple[bool, bool, bool]] = {}
        # Return an exception to make a write fail (not logged); None lets it through.
        self.fail: Callable[[str], Exception | None] = lambda _text: None

    def session(self, dbname: str = "postgres", *, pgconn: object | None = None) -> Session:
        return Session(self, dbname, pgconn)

    def snapshot(self) -> tuple[Any, ...]:
        return (
            sorted(self.roles),
            sorted(self.databases.items()),
            {k: sorted(v) for k, v in sorted(self.extensions.items())},
        )


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
        if text.startswith("SELECT 1 FROM pg_roles"):
            assert params is not None
            return Result([(1,)] if params[0] in c.roles else [])
        if "rolreplication" in text:
            assert params is not None
            return Result(
                [
                    (r, *c.role_attrs.get(r, (False, False, False)))
                    for r in sorted(c.roles)
                    if r in params[0]
                ]
            )
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
        elif m := re.match(r'CREATE DATABASE "(\w+)" OWNER "(\w+)"', text):
            assert m.group(1) not in c.databases, "database created twice"
            c.databases[m.group(1)] = m.group(2)
        elif m := re.match(r'ALTER DATABASE "(\w+)" OWNER TO "(\w+)"', text):
            c.databases[m.group(1)] = m.group(2)
        elif m := re.match(r'CREATE EXTENSION IF NOT EXISTS "(\w+)"', text):
            installed = c.extensions.setdefault(self.dbname, [])
            if m.group(1) not in installed:
                installed.append(m.group(1))
        return Result([])


def settings() -> AdminSettings:
    return AdminSettings.model_validate(
        {
            "POSTGRES_PASSWORD": SUPER_PW,
            "CAIRN_OWNER_DB_PASSWORD": OWNER_PW,
            "CAIRN_APP_DB_PASSWORD": APP_PW,
            "CAIRN_DBOS_DB_PASSWORD": DBOS_PW,
        }
    )


def run_bootstrap(
    cluster: Cluster, *, pgconn: object | None = None, **kwargs: Any
) -> BootstrapResult:
    admin = cluster.session(pgconn=pgconn)
    return bootstrap(
        cast("psycopg.Connection[Any]", admin),
        settings(),
        connect=lambda name: cast("psycopg.Connection[Any]", cluster.session(name)),
        **kwargs,
    )


def test_bootstrap_on_an_empty_cluster_runs_every_step_in_order() -> None:
    cluster = Cluster()
    pg = StubPg()
    result = run_bootstrap(cluster, pgconn=pg)
    kinds = [re.sub(r"^\[\w+\] ", "", line) for line in cluster.log]
    assert [k.split(" WITH")[0] for k in kinds[:3]] == [
        'CREATE ROLE "cairn_owner"',
        'CREATE ROLE "cairn_app"',
        'CREATE ROLE "cairn_dbos"',
    ]
    assert 'CREATE DATABASE "cairn" OWNER "cairn_owner"' in kinds
    assert 'CREATE DATABASE "cairn_dbos" OWNER "cairn_dbos"' in kinds
    # Roles first, then databases, then work inside the databases.
    assert kinds.index('CREATE DATABASE "cairn" OWNER "cairn_owner"') > 2
    assert cluster.extensions["cairn"] == list(REQUIRED_EXTENSIONS)
    assert "cairn_dbos" not in cluster.extensions
    # No schema grants: bootstrap probes and grants no extension schema (retarget FT-03).
    assert not any(k.startswith("GRANT USAGE ON SCHEMA") for k in kinds)
    # In-database statements ran on the database they belong to.
    assert any(line.startswith("[cairn] CREATE EXTENSION") for line in cluster.log)
    assert [r.created for r in result.roles] == [True, True, True]
    assert [(d.name, d.created) for d in result.databases] == [
        ("cairn", True),
        ("cairn_dbos", True),
    ]
    assert result.password_method == "libpq"
    assert [c[2] for c in pg.calls] == [b"scram-sha-256"] * 3


def test_bootstrap_twice_changes_nothing_in_the_catalog() -> None:
    cluster = Cluster()
    run_bootstrap(cluster, pgconn=StubPg())
    after_first = cluster.snapshot()
    first_writes = len(cluster.log)
    result = run_bootstrap(cluster, pgconn=StubPg())
    assert cluster.snapshot() == after_first
    second = cluster.log[first_writes:]
    assert not any("CREATE ROLE" in line or "CREATE DATABASE" in line for line in second)
    assert sum("ALTER ROLE" in line for line in second) == 3
    assert [r.created for r in result.roles] == [False, False, False]
    assert [d.created for d in result.databases] == [False, False]


def test_bootstrap_fixes_a_database_with_the_wrong_owner() -> None:
    cluster = Cluster()
    cluster.databases["cairn"] = "postgres"
    result = run_bootstrap(cluster, pgconn=StubPg())
    assert cluster.databases["cairn"] == OWNER_ROLE
    assert result.databases[0].owner_changed
    assert not result.databases[0].created


@pytest.mark.parametrize("member", [APP_ROLE, DBOS_ROLE])
def test_bootstrap_refuses_a_runtime_role_that_is_a_member_and_changes_nothing(member: str) -> None:
    cluster = Cluster()
    cluster.roles = {OWNER_ROLE, APP_ROLE, DBOS_ROLE}
    cluster.memberships = [(member, "pg_monitor"), (OWNER_ROLE, "pg_read_all_data")]
    with pytest.raises(BootstrapError) as err:
        run_bootstrap(cluster, pgconn=StubPg())
    assert f"{member} is a member of pg_monitor" in str(err.value)
    assert OWNER_ROLE not in str(err.value)
    assert cluster.log == []
    assert not any(secret in str(err.value) for secret in ALL_SECRETS)


def test_bootstrap_needs_an_autocommit_connection() -> None:
    cluster = Cluster()
    admin = cluster.session()
    admin.autocommit = False
    with pytest.raises(ValueError, match="autocommit"):
        bootstrap(cast("psycopg.Connection[Any]", admin), settings())
    assert cluster.log == []


def test_bootstrap_uses_the_python_verifier_when_libpq_is_unavailable() -> None:
    cluster = Cluster()
    result = run_bootstrap(cluster, pgconn=None)
    assert result.password_method == "python"
    role_lines = [line for line in cluster.log if "ROLE" in line]
    assert len(role_lines) == 3
    assert all(re.search(r"PASSWORD 'SCRAM-SHA-256\$4096:", line) for line in role_lines)


def test_no_plaintext_password_reaches_statements_logs_results_or_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    for pgconn in (StubPg(), None):  # libpq path and fallback path
        cluster = Cluster()
        result = run_bootstrap(cluster, pgconn=pgconn)
        blobs = [*cluster.log, repr(result), *describe(result)]
        for blob in blobs:
            assert not any(secret in blob for secret in ALL_SECRETS), blob
    text = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert "bootstrap.role" in text
    assert "bootstrap.database" in text
    assert not any(secret in text for secret in ALL_SECRETS)


def test_a_failing_statement_error_does_not_carry_a_password() -> None:
    class Failing(Session):
        def execute(self, query: object, params: tuple[Any, ...] | None = None) -> Result:
            if "CREATE ROLE" in (query.as_string() if isinstance(query, sql.Composable) else ""):
                raise psycopg.errors.InsufficientPrivilege("permission denied to create role")
            return super().execute(query, params)

    cluster = Cluster()
    admin = Failing(cluster, "postgres", StubPg())
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as err:
        bootstrap(cast("psycopg.Connection[Any]", admin), settings())
    assert not any(secret in str(err.value) for secret in ALL_SECRETS)


def test_bootstrap_database_builds_a_test_database_for_owner_and_app() -> None:
    cluster = Cluster()
    cluster.roles = {OWNER_ROLE, APP_ROLE, DBOS_ROLE}
    admin = cluster.session()
    result = bootstrap_database(
        cast("psycopg.Connection[Any]", admin),
        "cairn_test_ab12cd34",
        connect=lambda name: cast("psycopg.Connection[Any]", cluster.session(name)),
    )
    assert result == DatabaseResult("cairn_test_ab12cd34", True, False, REQUIRED_EXTENSIONS)
    assert cluster.databases == {"cairn_test_ab12cd34": OWNER_ROLE}
    assert cluster.extensions["cairn_test_ab12cd34"] == list(REQUIRED_EXTENSIONS)
    again = bootstrap_database(
        cast("psycopg.Connection[Any]", admin),
        "cairn_test_ab12cd34",
        connect=lambda name: cast("psycopg.Connection[Any]", cluster.session(name)),
    )
    assert not again.created


def test_bootstrap_database_accepts_a_custom_spec_and_checks_the_name() -> None:
    cluster = Cluster()
    admin = cast("psycopg.Connection[Any]", cluster.session())
    spec = DatabaseSpec("spike", OWNER_ROLE, (OWNER_ROLE, APP_ROLE), ("vector",))
    bootstrap_database(
        admin,
        "spike",
        spec=spec,
        connect=lambda n: cast("psycopg.Connection[Any]", cluster.session(n)),
    )
    assert cluster.extensions == {"spike": ["vector"]}
    with pytest.raises(ValueError, match=r"spec\.name"):
        bootstrap_database(admin, "other", spec=spec)


def test_describe_prints_stable_lines() -> None:
    result = BootstrapResult(
        roles=(RoleResult(OWNER_ROLE, True), RoleResult(APP_ROLE, False)),
        databases=(
            DatabaseResult("cairn", True, False, REQUIRED_EXTENSIONS),
            DatabaseResult("cairn_dbos", False, False, ()),
        ),
        password_method="libpq",
    )
    assert list(describe(result)) == [
        "admin mode: superuser",
        "role cairn_owner: created",
        "role cairn_app: updated",
        "database cairn: created",
        "extensions cairn: btree_gist, vector",
        "database cairn_dbos: exists",
        "bootstrap complete (password encryption: libpq)",
    ]


# --- cairn-db bootstrap / init ------------------------------------------------------------


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in env_keys(AdminSettings):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("POSTGRES_PASSWORD", SUPER_PW)
    monkeypatch.setenv("CAIRN_OWNER_DB_PASSWORD", OWNER_PW)
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", APP_PW)
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", DBOS_PW)


class Closing:
    def __enter__(self) -> Closing:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def patch_cli(
    monkeypatch: pytest.MonkeyPatch, events: list[str], *, bootstrap_error: Exception | None = None
) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def connect_as(
        _: AdminSettings, *, user: str, password: str, dbname: str | None = None
    ) -> Closing:
        seen.append({"user": user, "password": password, "dbname": dbname})
        events.append(f"connect:{user}")
        return Closing()

    def run(
        _conn: object, _settings: AdminSettings, *, allow_plaintext: bool = False
    ) -> BootstrapResult:
        events.append("bootstrap")
        if bootstrap_error is not None:
            raise bootstrap_error
        return BootstrapResult(
            (RoleResult(OWNER_ROLE, True),),
            (DatabaseResult("cairn", True, False, REQUIRED_EXTENSIONS),),
            "libpq",
        )

    def fake_migrate(_conn: object, migrations: Any) -> list[int]:
        events.append("migrate")
        return [m.version for m in migrations]

    monkeypatch.setattr(cli, "connect_as", connect_as)
    monkeypatch.setattr(cli, "run_bootstrap", run)
    monkeypatch.setattr(cli, "migrate", fake_migrate)
    return seen


def test_cli_bootstrap_connects_as_the_superuser_and_prints_stable_lines(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    seen = patch_cli(monkeypatch, events)
    result = CliRunner().invoke(cli.main, ["bootstrap"])
    assert result.exit_code == 0, result.output
    assert seen == [{"user": "postgres", "password": SUPER_PW, "dbname": "postgres"}]
    assert "role cairn_owner: created" in result.output
    assert "database cairn: created" in result.output
    assert "bootstrap complete" in result.output
    assert events == ["connect:postgres", "bootstrap"]
    assert not any(secret in result.output for secret in ALL_SECRETS)


def test_cli_init_bootstraps_then_migrates_as_the_owner(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    seen = patch_cli(monkeypatch, events)
    result = CliRunner().invoke(cli.main, ["init"])
    assert result.exit_code == 0, result.output
    assert events == ["connect:postgres", "bootstrap", "connect:cairn_owner", "migrate"]
    assert [s["user"] for s in seen] == ["postgres", "cairn_owner"]
    assert result.output.index("bootstrap complete") < result.output.index(
        "applied 0001_baseline.sql"
    )
    count = len(bundled_migrations())
    assert f"applied {count} migration(s); now at version {count}" in result.output


def test_cli_init_stops_when_bootstrap_fails(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    patch_cli(
        monkeypatch, events, bootstrap_error=BootstrapError("cairn_app is a member of pg_monitor")
    )
    result = CliRunner().invoke(cli.main, ["init"])
    assert result.exit_code == 1
    assert "bootstrap failed: cairn_app is a member of pg_monitor" in result.output
    assert "migrate" not in events


def test_cli_bootstrap_exits_two_naming_the_missing_superuser_key(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD")
    events: list[str] = []
    patch_cli(monkeypatch, events)
    for command in ("bootstrap", "init"):
        result = CliRunner().invoke(cli.main, [command])
        assert result.exit_code == 2
        assert "POSTGRES_PASSWORD" in result.output
        assert not any(secret in result.output for secret in ALL_SECRETS)
    assert events == []


def test_cli_bootstrap_exits_one_on_a_database_error_without_details(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise psycopg.OperationalError(f"connection to server at db failed password={SUPER_PW}")

    monkeypatch.setattr(cli, "connect_as", refuse)
    result = CliRunner().invoke(cli.main, ["bootstrap"])
    assert result.exit_code == 1
    assert "database error: OperationalError" in result.output
    assert SUPER_PW not in result.output
    assert "server at db" not in result.output


def test_cli_lists_the_new_commands() -> None:
    result = CliRunner().invoke(cli.main, ["--help"])
    assert result.exit_code == 0
    for command in ("bootstrap", "init", "migrate", "status"):
        assert command in result.output


# --- admin settings and TLS at the connection sites (free-tier FT-14, Requirement 4.4) ------


def test_cli_bootstrap_connects_as_the_configured_admin_to_the_admin_database(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_DB_ADMIN_USER", "neondb_owner")
    monkeypatch.setenv("CAIRN_DB_ADMIN_DATABASE", "neondb")
    monkeypatch.setenv("CAIRN_DB_ADMIN_PASSWORD", "admin-pw-FAKE-1")
    events: list[str] = []
    seen = patch_cli(monkeypatch, events)
    result = CliRunner().invoke(cli.main, ["bootstrap"])
    assert result.exit_code == 0, result.output
    # CAIRN_DB_ADMIN_PASSWORD wins over POSTGRES_PASSWORD; nothing names postgres.
    assert seen == [{"user": "neondb_owner", "password": "admin-pw-FAKE-1", "dbname": "neondb"}]
    assert "admin-pw-FAKE-1" not in result.output


def test_cli_missing_admin_password_names_both_keys(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD")
    patch_cli(monkeypatch, [])
    result = CliRunner().invoke(cli.main, ["bootstrap"])
    assert result.exit_code == 2
    assert "CAIRN_DB_ADMIN_PASSWORD or POSTGRES_PASSWORD (missing)" in result.output


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, {"sslmode": "prefer"}),
        (
            {"CAIRN_DB_SSLMODE": "verify-full", "CAIRN_DB_SSLROOTCERT": "/certs/root.crt"},
            {"sslmode": "verify-full", "sslrootcert": "/certs/root.crt"},
        ),
    ],
)
def test_connect_as_passes_the_tls_settings_to_psycopg(
    admin_env: None,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    expected: dict[str, str],
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    captured: list[dict[str, Any]] = []

    def fake_connect(**kwargs: Any) -> str:
        captured.append(kwargs)
        return "connection"

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    cli.connect_as(AdminSettings(), user=OWNER_ROLE, password=OWNER_PW)
    assert len(captured) == 1
    assert {k: captured[0][k] for k in ("sslmode", "sslrootcert") if k in captured[0]} == expected
    assert captured[0]["user"] == OWNER_ROLE


class FakeInfo:
    host = "db.example"
    port = 5432
    user = "neondb_owner"
    password = SUPER_PW

    def __init__(self, params: dict[str, str]) -> None:
        self._params = params

    def get_parameters(self) -> dict[str, str]:
        return dict(self._params)


class FakeAdminConn:
    def __init__(self, params: dict[str, str]) -> None:
        self.info = FakeInfo(params)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        # libpq omits defaults, so sslmode=prefer is absent and stays the default.
        ({"dbname": "neondb", "connect_timeout": "5"}, {}),
        (
            {"sslmode": "verify-full", "sslrootcert": "/certs/root.crt", "dbname": "neondb"},
            {"sslmode": "verify-full", "sslrootcert": "/certs/root.crt"},
        ),
        ({"sslmode": "require"}, {"sslmode": "require"}),
    ],
)
def test_connect_like_copies_the_admin_connection_tls_settings(
    monkeypatch: pytest.MonkeyPatch, params: dict[str, str], expected: dict[str, str]
) -> None:
    from cairn_core import bootstrap as bootstrap_module

    admin = cast("psycopg.Connection[Any]", FakeAdminConn(params))
    assert tls_parameters(admin) == expected
    captured: list[dict[str, Any]] = []

    def fake_connect(**kwargs: Any) -> str:
        captured.append(kwargs)
        return "connection"

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    bootstrap_module.connect_like(admin, "cairn")
    (kwargs,) = captured
    assert {k: kwargs[k] for k in ("sslmode", "sslrootcert") if k in kwargs} == expected
    assert (kwargs["user"], kwargs["dbname"], kwargs["host"]) == (
        "neondb_owner",
        "cairn",
        "db.example",
    )


# --- managed mode (free-tier FT-06, FT-07, FT-08; Requirements 5.1 to 5.12) --------------------

ADMIN = "cairn_admin"
"""A stand-in for ``neondb_owner``: any non-superuser admin name works."""
ADMIN_PW = "admin-pw-FAKE-Vb38nQ5zXe41"
MANAGED_SECRETS = (*ALL_SECRETS, ADMIN_PW)
MANAGED_ADMIN_ROW = (ADMIN, False, True, True, True)


def facts(**over: Any) -> AdminFacts:
    base = {
        "user": ADMIN,
        "is_superuser": False,
        "can_create_role": True,
        "can_create_db": True,
        "bypasses_rls": True,
    }
    return AdminFacts(**{**base, **over})


def managed_settings(**over: str) -> AdminSettings:
    return AdminSettings.model_validate(
        {
            "CAIRN_DB_ADMIN_USER": ADMIN,
            "CAIRN_DB_ADMIN_PASSWORD": ADMIN_PW,
            "CAIRN_DB_ADMIN_MODE": "managed",
            "CAIRN_OWNER_DB_PASSWORD": OWNER_PW,
            "CAIRN_APP_DB_PASSWORD": APP_PW,
            "CAIRN_DBOS_DB_PASSWORD": DBOS_PW,
            **over,
        }
    )


def managed_cluster() -> Cluster:
    cluster = Cluster()
    cluster.admin_row = MANAGED_ADMIN_ROW
    return cluster


def run_managed(cluster: Cluster, *, allow_plaintext: bool = False, **over: str) -> BootstrapResult:
    return bootstrap(
        cast("psycopg.Connection[Any]", cluster.session(pgconn=StubPg())),
        managed_settings(**over),
        connect=lambda name: cast("psycopg.Connection[Any]", cluster.session(name)),
        allow_plaintext=allow_plaintext,
    )


def writes(cluster: Cluster) -> list[str]:
    """The statements bootstrap executed, without the ``[database]`` prefix."""
    return [re.sub(r"^\[\w+\] ", "", line) for line in cluster.log]


# mode resolution


def test_read_admin_facts_maps_the_catalog_row() -> None:
    cluster = managed_cluster()
    assert read_admin_facts(cast("psycopg.Connection[Any]", cluster.session())) == facts()
    cluster.admin_row = ("postgres", True, True, True, True)
    assert read_admin_facts(cast("psycopg.Connection[Any]", cluster.session())).is_superuser


def test_auto_picks_superuser_for_a_superuser_and_managed_otherwise() -> None:
    assert resolve_mode("auto", facts(is_superuser=True)) == "superuser"
    assert resolve_mode("auto", facts()) == "managed"
    assert resolve_mode("superuser", facts(is_superuser=True)) == "superuser"
    assert resolve_mode("managed", facts()) == "managed"
    # A superuser can be forced into the managed code path.
    assert resolve_mode("managed", facts(is_superuser=True)) == "managed"


def test_superuser_mode_on_a_non_superuser_is_refused_naming_the_role() -> None:
    with pytest.raises(BootstrapError, match=ADMIN):
        resolve_mode("superuser", facts())


@pytest.mark.parametrize("requested", ["auto", "managed"])
@pytest.mark.parametrize(
    ("over", "named"),
    [
        ({"can_create_role": False}, ["CREATEROLE"]),
        ({"can_create_db": False}, ["CREATEDB"]),
        ({"can_create_role": False, "can_create_db": False}, ["CREATEROLE", "CREATEDB"]),
    ],
)
def test_managed_mode_names_the_missing_attribute(
    requested: Any, over: dict[str, bool], named: list[str]
) -> None:
    with pytest.raises(BootstrapError) as err:
        resolve_mode(requested, facts(**over))
    message = str(err.value)
    assert ADMIN in message
    assert all(attribute in message.split("lacks")[1] for attribute in named)


# role statements


def test_managed_create_names_every_attribute() -> None:
    text = render(role_statement(OWNER_ROLE, GOOD_VERIFIER, exists=False, mode="managed"))
    assert text == render(role_statement(OWNER_ROLE, GOOD_VERIFIER, exists=False))
    assert " NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS " in text


@pytest.mark.parametrize(
    ("role", "inherit"), [(OWNER_ROLE, "INHERIT"), (APP_ROLE, "NOINHERIT"), (DBOS_ROLE, "INHERIT")]
)
def test_managed_alter_leaves_out_the_clauses_a_non_superuser_may_not_name(
    role: str, inherit: str
) -> None:
    text = render(role_statement(role, GOOD_VERIFIER, exists=True, mode="managed"))
    assert text == (
        f'ALTER ROLE "{role}" WITH LOGIN NOCREATEDB NOCREATEROLE {inherit} '
        f"PASSWORD '{GOOD_VERIFIER}'"
    )
    for attribute in ("SUPERUSER", "REPLICATION", "BYPASSRLS"):
        assert attribute not in text


def test_superuser_alter_still_names_every_attribute() -> None:
    text = render(role_statement(APP_ROLE, GOOD_VERIFIER, exists=True, mode="superuser"))
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT" in text


def test_role_statements_pass_the_mode_to_every_role() -> None:
    texts = [
        render(s)
        for s in role_statements(
            PASSWORDS, set(MANAGED_ROLES_FOR_TEST), fake_encrypt, mode="managed"
        )
    ]
    assert all(t.startswith("ALTER ROLE") and "BYPASSRLS" not in t for t in texts)


MANAGED_ROLES_FOR_TEST = (OWNER_ROLE, APP_ROLE, DBOS_ROLE)


def test_role_attribute_violations_name_role_and_attribute() -> None:
    rows = [
        RoleRow(DBOS_ROLE, False, False, True),
        RoleRow(APP_ROLE, True, True, False),
        RoleRow(OWNER_ROLE, False, False, False),
        RoleRow("someone_else", True, True, True),  # not a managed role: ignored
    ]
    assert role_attribute_violations(rows) == [
        "cairn_app has SUPERUSER",
        "cairn_app has REPLICATION",
        "cairn_dbos has BYPASSRLS",
    ]
    assert role_attribute_violations([]) == []
    assert role_attribute_violations([RoleRow(OWNER_ROLE, False, False, False)]) == []


def test_admin_set_grants_are_set_only_for_both_database_owners() -> None:
    assert [render(s) for s in admin_set_grants(ADMIN)] == [
        'GRANT "cairn_owner" TO "cairn_admin" WITH INHERIT FALSE, SET TRUE',
        'GRANT "cairn_dbos" TO "cairn_admin" WITH INHERIT FALSE, SET TRUE',
    ]
    assert 'TO "Odd-Admin"' in render(admin_set_grants("Odd-Admin")[0])
    assert 'TO "x""; DROP ROLE y; --"' in render(admin_set_grants('x"; DROP ROLE y; --')[0])
    with pytest.raises(BootstrapError):
        admin_set_grants("")


# database and extension statements


def test_superuser_database_statements_are_unchanged_by_the_mode_argument() -> None:
    spec = content_spec()
    assert database_statements(spec, current_owner=None) == database_statements(
        spec, current_owner=None, mode="superuser", admin_user=ADMIN
    )


def test_managed_database_statements_run_the_privileges_as_the_owner() -> None:
    texts = [
        render(s)
        for s in database_statements(
            content_spec(), current_owner=None, mode="managed", admin_user=ADMIN
        )
    ]
    assert texts == [
        'CREATE DATABASE "cairn" OWNER "cairn_owner"',
        'SET ROLE "cairn_owner"',
        'REVOKE CONNECT, TEMPORARY ON DATABASE "cairn" FROM PUBLIC',
        'REVOKE CONNECT ON DATABASE "cairn" FROM "cairn_dbos"',
        'GRANT CONNECT ON DATABASE "cairn" TO "cairn_owner"',
        'GRANT CONNECT ON DATABASE "cairn" TO "cairn_app"',
        'GRANT CONNECT ON DATABASE "cairn" TO "cairn_admin"',
        'GRANT CREATE ON DATABASE "cairn" TO "cairn_admin"',
        "RESET ROLE",
    ]


def test_managed_dbos_database_needs_connect_but_no_create_for_the_admin() -> None:
    texts = [
        render(s)
        for s in database_statements(
            dbos_spec(), current_owner="cairn_dbos", mode="managed", admin_user=ADMIN
        )
    ]
    assert texts[0] == 'SET ROLE "cairn_dbos"'
    assert 'GRANT CONNECT ON DATABASE "cairn_dbos" TO "cairn_admin"' in texts
    assert not any("GRANT CREATE" in t for t in texts)
    assert admin_access_revoke_statements(dbos_spec(), mode="managed", admin_user=ADMIN) == []


def test_managed_database_statements_fix_the_owner_as_the_admin_before_set_role() -> None:
    texts = [
        render(s)
        for s in database_statements(
            content_spec(), current_owner="someone", mode="managed", admin_user=ADMIN
        )
    ]
    assert texts[:2] == ['ALTER DATABASE "cairn" OWNER TO "cairn_owner"', 'SET ROLE "cairn_owner"']
    assert texts[-1] == "RESET ROLE"


def test_managed_database_statements_need_the_admin_name() -> None:
    with pytest.raises(BootstrapError):
        database_statements(content_spec(), current_owner=None, mode="managed")
    with pytest.raises(BootstrapError):
        admin_access_revoke_statements(content_spec(), mode="managed", admin_user=None)


def test_the_temporary_create_grant_is_taken_back_under_set_role() -> None:
    assert admin_access_revoke_statements(content_spec(), mode="superuser", admin_user=ADMIN) == []
    assert [
        render(s)
        for s in admin_access_revoke_statements(content_spec(), mode="managed", admin_user=ADMIN)
    ] == [
        'SET ROLE "cairn_owner"',
        'REVOKE CREATE ON DATABASE "cairn" FROM "cairn_admin"',
        "RESET ROLE",
    ]


def test_managed_extensions_are_created_as_the_admin_and_only_the_revoke_runs_as_the_owner() -> (
    None
):
    texts = [render(s) for s in extension_statements(content_spec(), mode="managed")]
    assert texts == [
        'CREATE EXTENSION IF NOT EXISTS "vector"',
        'CREATE EXTENSION IF NOT EXISTS "btree_gist"',
        'SET ROLE "cairn_owner"',
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        "RESET ROLE",
    ]
    assert [render(s) for s in extension_statements(dbos_spec(), mode="managed")] == [
        'SET ROLE "cairn_dbos"',
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        "RESET ROLE",
    ]


# password form (OQ-1)


@pytest.mark.parametrize("mode", ["superuser", "managed"])
@pytest.mark.parametrize("sslmode", ["disable", "prefer", "require", "verify-ca", "verify-full"])
def test_password_form_is_plaintext_only_with_the_flag_managed_mode_and_verify_full(
    mode: Any, sslmode: Any
) -> None:
    assert password_form(mode, allow_plaintext=False, sslmode=sslmode) == "verifier"
    if mode == "managed" and sslmode == "verify-full":
        assert password_form(mode, allow_plaintext=True, sslmode=sslmode) == "plaintext"
    else:
        with pytest.raises(PlaintextRefused, match="allow-plaintext-password"):
            password_form(mode, allow_plaintext=True, sslmode=sslmode)


def test_the_flag_problems_are_checked_from_the_requested_mode_before_connecting() -> None:
    assert plaintext_flag_problems("managed", "verify-full") == []
    assert plaintext_flag_problems("auto", "verify-full") == ["CAIRN_DB_ADMIN_MODE must be managed"]
    assert plaintext_flag_problems("superuser", "prefer") == [
        "CAIRN_DB_ADMIN_MODE must be managed",
        "CAIRN_DB_SSLMODE must be verify-full",
    ]
    assert plaintext_flag_problems("managed", "require") == ["CAIRN_DB_SSLMODE must be verify-full"]


def test_a_plaintext_role_statement_carries_the_password_and_nothing_else_changes() -> None:
    text = render(role_statement(APP_ROLE, APP_PW, exists=False, mode="managed", form="plaintext"))
    assert text == (
        'CREATE ROLE "cairn_app" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
        f"NOBYPASSRLS NOINHERIT PASSWORD '{APP_PW}'"
    )
    alter = render(role_statement(APP_ROLE, APP_PW, exists=True, mode="managed", form="plaintext"))
    assert alter == (
        f"ALTER ROLE \"cairn_app\" WITH LOGIN NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{APP_PW}'"
    )


def test_a_plaintext_password_is_quoted_as_a_literal() -> None:
    text = render(
        role_statement(APP_ROLE, "pa'ss\\w\"ord;--x", exists=True, mode="managed", form="plaintext")
    )
    assert text.endswith("PASSWORD 'pa''ss\\w\"ord;--x'") or "E'" in text
    assert "''" in text


def test_plaintext_form_is_refused_in_superuser_mode_and_for_verifier_lookalikes() -> None:
    with pytest.raises(PlaintextRefused) as err:
        role_statement(APP_ROLE, APP_PW, exists=False, mode="superuser", form="plaintext")
    assert APP_PW not in str(err.value)
    for bad in ("", GOOD_VERIFIER):
        with pytest.raises(BootstrapError):
            role_statement(APP_ROLE, bad, exists=False, mode="managed", form="plaintext")


def test_role_statements_in_plaintext_form_need_no_encryptor_and_send_each_password() -> None:
    texts = [
        render(s) for s in role_statements(PASSWORDS, (), None, mode="managed", form="plaintext")
    ]
    for text, password in zip(texts, (OWNER_PW, APP_PW, DBOS_PW), strict=True):
        assert text.endswith(f"PASSWORD '{password}'")
    with pytest.raises(BootstrapError):
        role_statements(PASSWORDS, (), None)  # verifier form needs an encryptor


# bootstrap() in managed mode against the fake cluster


def test_managed_bootstrap_runs_roles_then_grants_then_databases_in_order() -> None:
    cluster = managed_cluster()
    result = run_managed(cluster)
    assert (result.mode, result.password_form) == ("managed", "verifier")
    kinds = writes(cluster)
    assert [k.split(" WITH")[0] for k in kinds[:3]] == [
        'CREATE ROLE "cairn_owner"',
        'CREATE ROLE "cairn_app"',
        'CREATE ROLE "cairn_dbos"',
    ]
    assert kinds[3:5] == [
        'GRANT "cairn_owner" TO "cairn_admin" WITH INHERIT FALSE, SET TRUE',
        'GRANT "cairn_dbos" TO "cairn_admin" WITH INHERIT FALSE, SET TRUE',
    ]
    assert kinds[5] == 'CREATE DATABASE "cairn" OWNER "cairn_owner"'
    assert cluster.extensions["cairn"] == list(REQUIRED_EXTENSIONS)
    assert "cairn_dbos" not in cluster.extensions


def test_managed_bootstrap_scopes_set_role_around_the_right_statements() -> None:
    cluster = managed_cluster()
    run_managed(cluster)
    kinds = writes(cluster)
    # Walk the log: every REVOKE/GRANT on a database, and the public revoke, sits inside a
    # SET ROLE ... RESET ROLE pair; CREATE EXTENSION and CREATE DATABASE sit outside any.
    active: str | None = None
    for line in kinds:
        if line.startswith("SET ROLE"):
            assert active is None, "nested SET ROLE"
            active = line
        elif line == "RESET ROLE":
            assert active is not None
            active = None
        elif line.startswith(("CREATE DATABASE", "CREATE EXTENSION", "CREATE ROLE", "ALTER ROLE")):
            assert active is None, f"{line} must not run under SET ROLE"
        elif line.startswith(("REVOKE", "GRANT CONNECT", "GRANT CREATE")):
            assert active is not None, f"{line} must run under SET ROLE"
    assert active is None
    assert 'SET ROLE "cairn_dbos"' in kinds


def test_managed_bootstrap_drops_the_temporary_create_grant_after_the_extensions() -> None:
    cluster = managed_cluster()
    run_managed(cluster)
    kinds = writes(cluster)
    grant = kinds.index('GRANT CREATE ON DATABASE "cairn" TO "cairn_admin"')
    extension = kinds.index('CREATE EXTENSION IF NOT EXISTS "vector"')
    revoke = kinds.index('REVOKE CREATE ON DATABASE "cairn" FROM "cairn_admin"')
    assert grant < extension < revoke
    assert not any("cairn_dbos" in k and "GRANT CREATE" in k for k in kinds)


def test_a_failing_extension_still_revokes_the_create_grant_and_resets_the_role() -> None:
    cluster = managed_cluster()
    cluster.fail = lambda text: (
        psycopg.errors.InsufficientPrivilege("permission denied to create extension")
        if text.startswith('CREATE EXTENSION IF NOT EXISTS "vector"')
        else None
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run_managed(cluster)
    kinds = writes(cluster)
    assert 'REVOKE CREATE ON DATABASE "cairn" FROM "cairn_admin"' in kinds
    assert kinds[-1] == "RESET ROLE"


def test_managed_bootstrap_alters_existing_roles_without_the_forbidden_clauses() -> None:
    cluster = managed_cluster()
    cluster.roles = {OWNER_ROLE, APP_ROLE, DBOS_ROLE}
    cluster.databases = {"cairn": OWNER_ROLE, "cairn_dbos": DBOS_ROLE}
    run_managed(cluster)
    alters = [k for k in writes(cluster) if k.startswith("ALTER ROLE")]
    assert len(alters) == 3
    for text in alters:
        assert " WITH LOGIN NOCREATEDB NOCREATEROLE " in text
        assert not any(a in text for a in ("SUPERUSER", "REPLICATION", "BYPASSRLS"))


def test_managed_bootstrap_twice_changes_nothing_in_the_catalog() -> None:
    cluster = managed_cluster()
    run_managed(cluster)
    after_first = cluster.snapshot()
    first = len(cluster.log)
    result = run_managed(cluster)
    assert cluster.snapshot() == after_first
    second = writes(cluster)[first:]
    assert not any("CREATE ROLE" in k or "CREATE DATABASE" in k for k in second)
    assert [r.created for r in result.roles] == [False, False, False]


@pytest.mark.parametrize(
    ("role", "attrs", "attribute"),
    [
        (APP_ROLE, (True, False, False), "SUPERUSER"),
        (OWNER_ROLE, (False, True, False), "REPLICATION"),
        (DBOS_ROLE, (False, False, True), "BYPASSRLS"),
    ],
)
def test_managed_bootstrap_refuses_a_forbidden_attribute_and_changes_nothing(
    role: str, attrs: tuple[bool, bool, bool], attribute: str
) -> None:
    cluster = managed_cluster()
    cluster.roles = {OWNER_ROLE, APP_ROLE, DBOS_ROLE}
    cluster.role_attrs = {role: attrs}
    with pytest.raises(BootstrapError) as err:
        run_managed(cluster)
    assert f"{role} has {attribute}" in str(err.value)
    assert cluster.log == []


def test_bootstrap_decides_the_mode_before_changing_anything() -> None:
    # Superuser mode requested for a non-superuser; managed mode without CREATEROLE.
    cluster = managed_cluster()
    with pytest.raises(BootstrapError, match="superuser"):
        run_managed(cluster, CAIRN_DB_ADMIN_MODE="superuser")
    cluster.admin_row = (ADMIN, False, False, True, False)
    with pytest.raises(BootstrapError, match="CREATEROLE"):
        run_managed(cluster)
    cluster.admin_row = (ADMIN, False, True, False, False)
    with pytest.raises(BootstrapError, match="CREATEDB"):
        run_managed(cluster, CAIRN_DB_ADMIN_MODE="auto")
    assert cluster.log == []


def test_auto_mode_on_a_superuser_keeps_the_superuser_path() -> None:
    cluster = Cluster()  # default admin row is a superuser
    result = run_bootstrap(cluster, pgconn=StubPg())
    assert result.mode == "superuser"
    kinds = writes(cluster)
    assert not any("ROLE" in k and not k.startswith(("CREATE ROLE", "ALTER ROLE")) for k in kinds)
    assert not any("cairn_admin" in k or k.startswith('GRANT "') for k in kinds)
    assert not any(k.startswith("GRANT CREATE ON DATABASE") for k in kinds)


def test_managed_bootstrap_with_the_flag_sends_plaintext_in_the_role_statements_only() -> None:
    cluster = managed_cluster()
    result = run_managed(cluster, allow_plaintext=True, CAIRN_DB_SSLMODE="verify-full")
    assert (result.mode, result.password_form, result.password_method) == (
        "managed",
        "plaintext",
        "unused",
    )
    role_lines = [k for k in writes(cluster) if "ROLE" in k and "PASSWORD" in k]
    assert len(role_lines) == 3
    for line, password in zip(role_lines, (OWNER_PW, APP_PW, DBOS_PW), strict=True):
        assert line.endswith(f"PASSWORD '{password}'")
    other = [k for k in writes(cluster) if k not in role_lines]
    assert not any(secret in k for k in other for secret in MANAGED_SECRETS)
    assert any("plaintext" in line for line in describe(result))
    assert not any(secret in line for line in describe(result) for secret in MANAGED_SECRETS)
    assert not any(secret in repr(result) for secret in MANAGED_SECRETS)


@pytest.mark.parametrize(
    "over",
    [
        {"CAIRN_DB_SSLMODE": "prefer"},
        {"CAIRN_DB_SSLMODE": "require"},
        {"CAIRN_DB_SSLMODE": "verify-ca"},
        {"CAIRN_DB_ADMIN_MODE": "superuser", "CAIRN_DB_SSLMODE": "verify-full"},
    ],
)
def test_the_flag_is_refused_by_bootstrap_before_any_change(over: dict[str, str]) -> None:
    cluster = managed_cluster()
    cluster.admin_row = ("postgres", True, True, True, True)  # lets superuser mode resolve
    with pytest.raises(PlaintextRefused) as err:
        run_managed(cluster, allow_plaintext=True, **over)
    assert cluster.log == []
    assert not any(secret in str(err.value) for secret in MANAGED_SECRETS)


def test_without_the_flag_managed_mode_never_puts_a_plaintext_password_anywhere(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    cluster = managed_cluster()
    result = run_managed(cluster, CAIRN_DB_SSLMODE="verify-full")
    assert result.password_form == "verifier"
    blobs = [*cluster.log, repr(result), *describe(result), caplog.text]
    blobs += [str(r.__dict__) for r in caplog.records]
    for blob in blobs:
        assert not any(secret in blob for secret in MANAGED_SECRETS), blob
    assert "bootstrap.mode" in caplog.text


def test_with_the_flag_no_log_line_result_or_error_holds_a_plaintext_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    cluster = managed_cluster()
    result = run_managed(cluster, allow_plaintext=True, CAIRN_DB_SSLMODE="verify-full")
    text = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert "bootstrap.role" in text
    assert not any(secret in text for secret in MANAGED_SECRETS)
    assert not any(secret in repr(result) for secret in MANAGED_SECRETS)
    # A server error on the one statement that carries the password reports the server's
    # message only, and the CLI prints the class and SQLSTATE.
    failing = managed_cluster()
    failing.fail = lambda t: (
        psycopg.errors.InvalidParameterValue("password is too weak")
        if t.startswith("CREATE ROLE")
        else None
    )
    with pytest.raises(psycopg.Error) as err:
        run_managed(failing, allow_plaintext=True, CAIRN_DB_SSLMODE="verify-full")
    assert not any(secret in str(err.value) for secret in MANAGED_SECRETS)


# the emulation


def test_emulation_statements_create_the_admin_and_the_template_extensions() -> None:
    statements = emulate_managed_admin_statements(GOOD_VERIFIER, exists=False)
    assert [render(s) for s in statements] == [
        'CREATE ROLE "cairn_admin" WITH LOGIN NOSUPERUSER CREATEDB CREATEROLE NOREPLICATION '
        f"BYPASSRLS PASSWORD '{GOOD_VERIFIER}'",
        'CREATE EXTENSION IF NOT EXISTS "vector"',
        'CREATE EXTENSION IF NOT EXISTS "btree_gist"',
    ]
    assert render(emulate_managed_admin_statements(GOOD_VERIFIER, exists=True)[0]).startswith(
        'ALTER ROLE "cairn_admin" WITH LOGIN NOSUPERUSER CREATEDB CREATEROLE'
    )


def test_emulation_statements_refuse_a_plaintext_password() -> None:
    with pytest.raises(BootstrapError) as err:
        emulate_managed_admin_statements(ADMIN_PW, exists=False)
    assert ADMIN_PW not in str(err.value)


def emulation_settings(**over: str) -> EmulationSettings:
    return EmulationSettings.model_validate(
        {
            "CAIRN_ENV": "ci",
            "POSTGRES_PASSWORD": SUPER_PW,
            "CAIRN_MANAGED_ADMIN_PASSWORD": ADMIN_PW,
            **over,
        }
    )


def run_emulation(cluster: Cluster, **over: str) -> EmulationResult:
    return emulate_managed_admin(
        cast("psycopg.Connection[Any]", cluster.session(pgconn=StubPg())),
        emulation_settings(**over),
        connect=lambda name: cast("psycopg.Connection[Any]", cluster.session(name)),
    )


@pytest.mark.parametrize("env", ["local", "ci"])
def test_emulation_creates_the_admin_and_the_template_extensions(env: str) -> None:
    cluster = Cluster()
    result = run_emulation(cluster, CAIRN_ENV=env)
    assert result == EmulationResult(True, REQUIRED_EXTENSIONS)
    kinds = writes(cluster)
    assert kinds[0].startswith(
        'CREATE ROLE "cairn_admin" WITH LOGIN NOSUPERUSER CREATEDB CREATEROLE '
        "NOREPLICATION BYPASSRLS"
    )
    assert "PASSWORD 'SCRAM-SHA-256$" in kinds[0]
    assert cluster.extensions == {"template1": list(REQUIRED_EXTENSIONS)}
    assert [line.split("]")[0] for line in cluster.log] == ["[postgres", "[template1", "[template1"]
    assert list(describe_emulation(result)) == [
        "role cairn_admin: created",
        "extensions template1: btree_gist, vector",
        "emulation complete",
    ]


def test_emulation_run_twice_alters_the_role_and_adds_nothing() -> None:
    cluster = Cluster()
    run_emulation(cluster)
    after_first = cluster.snapshot()
    second = run_emulation(cluster)
    assert second.role_created is False
    assert cluster.snapshot() == after_first
    assert writes(cluster)[3].startswith('ALTER ROLE "cairn_admin"')


def test_emulation_guard_refuses_the_demo_environment_before_touching_the_server() -> None:
    settings = emulation_settings(CAIRN_ENV="demo")
    assert emulation_refusals(settings) == ["CAIRN_ENV must be local or ci"]
    assert emulation_refusals(emulation_settings(CAIRN_ENV="ci")) == []
    cluster = Cluster()
    with pytest.raises(BootstrapError, match="CAIRN_ENV"):
        run_emulation(cluster, CAIRN_ENV="demo")
    assert cluster.log == []


def test_emulation_guard_refuses_a_server_with_a_neondb_owner_role() -> None:
    cluster = Cluster()
    cluster.roles = {"neondb_owner"}
    with pytest.raises(BootstrapError, match="neondb_owner"):
        run_emulation(cluster)
    assert cluster.log == []


def test_emulation_needs_a_superuser_admin() -> None:
    cluster = managed_cluster()
    with pytest.raises(BootstrapError, match="superuser"):
        run_emulation(cluster)
    assert cluster.log == []


def test_emulation_never_holds_a_plaintext_password_in_a_statement_log_or_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    cluster = Cluster()
    result = run_emulation(cluster)
    blobs = [*cluster.log, repr(result), *describe_emulation(result), caplog.text]
    for blob in blobs:
        assert not any(secret in blob for secret in MANAGED_SECRETS), blob
    assert "bootstrap.emulation" in caplog.text


def test_emulation_settings_need_the_new_admin_password_and_no_role_passwords(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in env_keys(EmulationSettings):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", SUPER_PW)
    monkeypatch.setenv("CAIRN_MANAGED_ADMIN_PASSWORD", ADMIN_PW)
    settings = EmulationSettings()
    assert settings.admin_user == "postgres"
    assert settings.managed_admin_password.get_secret_value() == ADMIN_PW
    assert "CAIRN_OWNER_DB_PASSWORD" not in env_keys(EmulationSettings)
    assert "CAIRN_MANAGED_ADMIN_PASSWORD" in env_keys(EmulationSettings)


# the CLI: the plaintext flag and emulate-managed-admin


def managed_cli_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    monkeypatch.setenv("CAIRN_DB_ADMIN_USER", ADMIN)
    monkeypatch.setenv("CAIRN_DB_ADMIN_PASSWORD", ADMIN_PW)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("command", ["bootstrap", "init"])
@pytest.mark.parametrize(
    "env",
    [
        {},  # auto mode, sslmode prefer
        {"CAIRN_DB_ADMIN_MODE": "managed"},  # sslmode prefer
        {"CAIRN_DB_ADMIN_MODE": "managed", "CAIRN_DB_SSLMODE": "require"},
        {"CAIRN_DB_ADMIN_MODE": "managed", "CAIRN_DB_SSLMODE": "verify-ca"},
        {"CAIRN_DB_ADMIN_MODE": "superuser", "CAIRN_DB_SSLMODE": "verify-full"},
        {"CAIRN_DB_ADMIN_MODE": "auto", "CAIRN_DB_SSLMODE": "verify-full"},
    ],
)
def test_cli_refuses_the_plaintext_flag_with_exit_two_before_connecting(
    admin_env: None, monkeypatch: pytest.MonkeyPatch, command: str, env: dict[str, str]
) -> None:
    managed_cli_env(monkeypatch, **env)
    events: list[str] = []
    patch_cli(monkeypatch, events)
    result = CliRunner().invoke(cli.main, [command, "--allow-plaintext-password"])
    assert result.exit_code == 2, result.output
    assert "--allow-plaintext-password refused" in result.output
    assert events == []  # nothing connected, nothing bootstrapped or migrated
    assert not any(secret in result.output for secret in MANAGED_SECRETS)


@pytest.mark.parametrize("command", ["bootstrap", "init"])
def test_cli_passes_the_accepted_flag_to_bootstrap(
    admin_env: None, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    managed_cli_env(monkeypatch, CAIRN_DB_ADMIN_MODE="managed", CAIRN_DB_SSLMODE="verify-full")
    passed: list[bool] = []
    events: list[str] = []
    patch_cli(monkeypatch, events)

    def run(
        _conn: object, _settings: AdminSettings, *, allow_plaintext: bool = False
    ) -> BootstrapResult:
        passed.append(allow_plaintext)
        return BootstrapResult(
            (RoleResult(OWNER_ROLE, True),),
            (DatabaseResult("cairn", True, False, REQUIRED_EXTENSIONS),),
            "unused",
            "managed",
            "plaintext",
        )

    monkeypatch.setattr(cli, "run_bootstrap", run)
    result = CliRunner().invoke(cli.main, [command, "--allow-plaintext-password"])
    assert result.exit_code == 0, result.output
    assert passed == [True]
    assert "admin mode: managed" in result.output
    assert not any(secret in result.output for secret in MANAGED_SECRETS)
    # Without the flag, the same settings pass False.
    passed.clear()
    assert CliRunner().invoke(cli.main, [command]).exit_code == 0
    assert passed == [False]


def test_cli_maps_a_refused_flag_raised_inside_bootstrap_to_exit_two(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_cli_env(monkeypatch, CAIRN_DB_ADMIN_MODE="managed", CAIRN_DB_SSLMODE="verify-full")
    patch_cli(
        monkeypatch, [], bootstrap_error=PlaintextRefused("--allow-plaintext-password refused")
    )
    result = CliRunner().invoke(cli.main, ["bootstrap", "--allow-plaintext-password"])
    assert result.exit_code == 2
    assert "--allow-plaintext-password refused" in result.output


def test_cli_lists_the_flag_and_the_emulation_command() -> None:
    assert "emulate-managed-admin" in CliRunner().invoke(cli.main, ["--help"]).output
    for command in ("bootstrap", "init"):
        out = CliRunner().invoke(cli.main, [command, "--help"]).output
        assert "--allow-plaintext-password" in out


@pytest.fixture
def emulation_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in env_keys(EmulationSettings) | env_keys(AdminSettings):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POSTGRES_PASSWORD", SUPER_PW)
    monkeypatch.setenv("CAIRN_MANAGED_ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("CAIRN_ENV", "ci")


def patch_emulation(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def connect_as(_: object, *, user: str, password: str, dbname: str | None = None) -> Closing:
        seen.append({"user": user, "password": password, "dbname": dbname})
        events.append(f"connect:{user}")
        return Closing()

    def run(_conn: object, settings_: EmulationSettings) -> EmulationResult:
        events.append("emulate")
        return EmulationResult(True, REQUIRED_EXTENSIONS)

    monkeypatch.setattr(cli, "connect_as", connect_as)
    monkeypatch.setattr(cli, "emulate_managed_admin", run)
    return seen


@pytest.mark.parametrize("env", ["local", "ci"])
def test_cli_emulate_managed_admin_connects_as_the_superuser_and_prints_stable_lines(
    emulation_env: None, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setenv("CAIRN_ENV", env)
    events: list[str] = []
    seen = patch_emulation(monkeypatch, events)
    result = CliRunner().invoke(cli.main, ["emulate-managed-admin"])
    assert result.exit_code == 0, result.output
    assert seen == [{"user": "postgres", "password": SUPER_PW, "dbname": "postgres"}]
    assert events == ["connect:postgres", "emulate"]
    assert "role cairn_admin: created" in result.output
    assert "extensions template1: btree_gist, vector" in result.output
    assert not any(secret in result.output for secret in MANAGED_SECRETS)


def test_cli_emulate_managed_admin_refuses_demo_before_connecting(
    emulation_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_ENV", "demo")
    events: list[str] = []
    patch_emulation(monkeypatch, events)
    result = CliRunner().invoke(cli.main, ["emulate-managed-admin"])
    assert result.exit_code == 1
    assert "CAIRN_ENV must be local or ci" in result.output
    assert events == []


def test_cli_emulate_managed_admin_reports_a_refusal_from_the_server_checks(
    emulation_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_emulation(monkeypatch, [])

    def refuse(_conn: object, _settings: object) -> EmulationResult:
        raise BootstrapError(
            "emulate-managed-admin refused: the server has a role named neondb_owner"
        )

    monkeypatch.setattr(cli, "emulate_managed_admin", refuse)
    result = CliRunner().invoke(cli.main, ["emulate-managed-admin"])
    assert result.exit_code == 1
    assert "neondb_owner" in result.output


def test_cli_emulate_managed_admin_exits_two_naming_the_missing_key(
    emulation_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAIRN_MANAGED_ADMIN_PASSWORD")
    events: list[str] = []
    patch_emulation(monkeypatch, events)
    result = CliRunner().invoke(cli.main, ["emulate-managed-admin"])
    assert result.exit_code == 2
    assert "CAIRN_MANAGED_ADMIN_PASSWORD" in result.output
    assert events == []
    monkeypatch.setenv("CAIRN_MANAGED_ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.delenv("POSTGRES_PASSWORD")
    result = CliRunner().invoke(cli.main, ["emulate-managed-admin"])
    assert result.exit_code == 2
    assert "CAIRN_DB_ADMIN_PASSWORD or POSTGRES_PASSWORD (missing)" in result.output
    assert not any(secret in result.output for secret in MANAGED_SECRETS)


def test_cli_emulate_managed_admin_exits_one_on_a_database_error_without_details(
    emulation_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise psycopg.OperationalError(f"connection failed password={SUPER_PW}")

    monkeypatch.setattr(cli, "connect_as", refuse)
    result = CliRunner().invoke(cli.main, ["emulate-managed-admin"])
    assert result.exit_code == 1
    assert "database error: OperationalError" in result.output
    assert SUPER_PW not in result.output
