"""Database test support: pytest fixtures and helpers (design §15, PD-16, Requirement 17.1).

The root ``conftest.py`` imports the fixtures from here:

- ``admin_config`` (session): where the test database is and how to log in.
- ``admin_conn`` (session): an autocommit admin connection to the compose ``db``
  service (a superuser) or, in managed mode, to ``ft-db`` (``cairn_admin``). It runs
  ``bootstrap`` once, which is idempotent, so a cluster that is already bootstrapped is
  left as it is.
- ``fresh_db`` (session): a throwaway ``cairn_test_<8 hex>`` database owned by
  ``cairn_owner``, bootstrapped and migrated, dropped when the session ends.

Settings come from the process environment first, then from ``.env`` in the working
directory: ``CAIRN_TEST_ADMIN_HOST`` / ``_PORT`` / ``_USER`` for the address and
``POSTGRES_PASSWORD`` for the superuser password (there is no separate test password),
plus the three role passwords that ``cairn-db`` already needs.

Two target modes (free-tier design §9 and §15), chosen by ``CAIRN_TEST_ADMIN_MODE``:
``superuser`` (the default, unset) is the compose ``db`` as above. ``managed`` is the
free-tier ``ft-db``: port ``CAIRN_FREETIER_DB_PORT``, user ``cairn_admin``, password
``CAIRN_MANAGED_ADMIN_PASSWORD`` (a NOSUPERUSER CREATEROLE CREATEDB BYPASSRLS role, like a
provider's owner role). ``CAIRN_TEST_ADMIN_HOST`` applies to both. Every connection and DSN here
carries ``CAIRN_DB_SSLMODE`` and ``CAIRN_DB_SSLROOTCERT`` (free-tier FT-14), and the admin
connection starts in ``CAIRN_DB_ADMIN_DATABASE``. No value is ever printed:
the dataclasses below keep connection strings out of ``repr``.

When the database is down or the settings are incomplete, the fixtures call
``pytest.skip`` with a message that says what to do. A reachable database that rejects
the login is a misconfiguration and fails loudly instead.

``temporary_database`` is the same machinery as a context manager, for tests that need
their own database (for example to edit the bookkeeping table) and for
``scripts/migrations_check.py``, which runs outside pytest.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cairn_core.bootstrap import (
    APP_ROLE,
    EDGE_ROLE,
    INGEST_ROLE,
    OWNER_ROLE,
    REQUIRED_EXTENSIONS,
    bootstrap,
    bootstrap_database,
    content_spec,
    read_admin_facts,
)
from cairn_core.config import AdminSettings, libpq_tls
from cairn_core.migrate import Migration, bundled_migrations, migrate

TEST_DB_PREFIX = "cairn_test_"
"""Every database these helpers create starts with this; they refuse to drop any other."""

CONNECT_TIMEOUT_S = 5


class DatabaseUnavailable(Exception):
    """The test database cannot be used. The message says what to do and holds no secret."""


AdminTargetMode = Literal["superuser", "managed"]
MANAGED_ADMIN_USER = "cairn_admin"
"""The login ``cairn-db emulate-managed-admin`` creates on ``ft-db`` (managed mode)."""


class AdminTarget(BaseSettings):
    """Where the test admin connects.

    ``superuser`` mode (``CAIRN_TEST_ADMIN_MODE`` unset): ``CAIRN_TEST_ADMIN_PORT`` and
    ``_USER`` with ``POSTGRES_PASSWORD``. ``managed`` mode: ``CAIRN_FREETIER_DB_PORT``, user
    ``cairn_admin`` and ``CAIRN_MANAGED_ADMIN_PASSWORD``. Read ``port``, ``user`` and
    ``password`` for the selected target; the other mode's keys are never used.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", frozen=True
    )
    mode: AdminTargetMode = Field("superuser", alias="CAIRN_TEST_ADMIN_MODE")
    host: str = Field("127.0.0.1", alias="CAIRN_TEST_ADMIN_HOST", min_length=1)
    superuser_port: int = Field(5432, alias="CAIRN_TEST_ADMIN_PORT", ge=1, le=65535)
    superuser_user: str = Field("postgres", alias="CAIRN_TEST_ADMIN_USER", min_length=1)
    superuser_password: SecretStr | None = Field(None, alias="POSTGRES_PASSWORD")
    freetier_port: int = Field(55433, alias="CAIRN_FREETIER_DB_PORT", ge=1, le=65535)
    managed_password: SecretStr | None = Field(None, alias="CAIRN_MANAGED_ADMIN_PASSWORD")

    @model_validator(mode="after")
    def _selected_password_is_set(self) -> AdminTarget:
        key, value = (
            ("CAIRN_MANAGED_ADMIN_PASSWORD", self.managed_password)
            if self.mode == "managed"
            else ("POSTGRES_PASSWORD", self.superuser_password)
        )
        if value is None or not value.get_secret_value():
            raise ValueError(f"{key} is required when CAIRN_TEST_ADMIN_MODE is {self.mode}")
        return self

    @property
    def port(self) -> int:
        return self.freetier_port if self.mode == "managed" else self.superuser_port

    @property
    def user(self) -> str:
        return MANAGED_ADMIN_USER if self.mode == "managed" else self.superuser_user

    @property
    def password(self) -> SecretStr:
        value = self.managed_password if self.mode == "managed" else self.superuser_password
        if value is None:  # the validator rules this out
            raise RuntimeError("test admin password is not set")
        return value


@dataclass(frozen=True)
class DbTestConfig:
    """Address and credentials for the test database. ``repr`` shows no secret."""

    target: AdminTarget
    settings: AdminSettings


def load_config() -> DbTestConfig:
    """Read the settings, or raise ``DatabaseUnavailable`` naming the keys that are wrong."""
    try:
        target = AdminTarget()  # fields load from the environment
        settings = AdminSettings()
    except ValidationError as exc:
        # Keys and error codes only: the input value can be a secret.
        found = {
            # A model-level check (the selected password) has no location: its message
            # names the key and never holds a value.
            f"{e['loc'][0]} ({e['type']})" if e["loc"] else e["msg"].removeprefix("Value error, ")
            for e in exc.errors(include_input=False, include_url=False, include_context=False)
        }
        raise DatabaseUnavailable(
            f"database settings are missing or invalid ({', '.join(sorted(found))}); run `make env`"
        ) from None
    return DbTestConfig(target=target, settings=settings)


def connect_admin(config: DbTestConfig, dbname: str | None = None) -> psycopg.Connection[Any]:
    """Autocommit admin connection to ``dbname`` (default ``CAIRN_DB_ADMIN_DATABASE``) with the
    ``CAIRN_DB_SSLMODE`` and ``CAIRN_DB_SSLROOTCERT`` settings. Raises ``DatabaseUnavailable``
    if nothing answers."""
    target = config.target
    try:
        return psycopg.connect(
            host=target.host,
            port=target.port,
            dbname=dbname or config.settings.admin_database,
            user=target.user,
            password=target.password.get_secret_value(),
            connect_timeout=CONNECT_TIMEOUT_S,
            application_name="cairn-tests",
            autocommit=True,
            **libpq_tls(config.settings),
        )
    except psycopg.OperationalError as exc:
        if exc.sqlstate and exc.sqlstate.startswith("28"):
            raise  # the server answered and refused the login: a real configuration error
        if target.mode == "managed":
            raise DatabaseUnavailable(
                f"cannot reach the managed-mode test database (ft-db) at "
                f"{target.host}:{target.port} ({type(exc).__name__}); start it with "
                "`make freetier-up`, or unset CAIRN_TEST_ADMIN_MODE to test the core db"
            ) from None
        raise DatabaseUnavailable(
            f"cannot reach the test database at {target.host}:{target.port} "
            f"({type(exc).__name__}); start it with `make up-db`"
        ) from None


@dataclass(frozen=True)
class FreshDb:
    """A throwaway test database. Connection strings carry passwords, so ``repr`` hides them."""

    name: str
    host: str
    port: int
    owner_dsn: str = field(repr=False)
    app_dsn: str = field(repr=False)
    admin_dsn: str = field(repr=False)
    ingest_dsn: str | None = field(default=None, repr=False)
    edge_dsn: str | None = field(default=None, repr=False)

    def connect_edge(self) -> psycopg.Connection[Any]:
        """Autocommit connection as ``cairn_edge`` (needs CAIRN_EDGE_DB_PASSWORD)."""
        if self.edge_dsn is None:
            raise RuntimeError("CAIRN_EDGE_DB_PASSWORD is not set; run `make env-update`")
        return psycopg.connect(self.edge_dsn, autocommit=True)

    def connect_ingest(self) -> psycopg.Connection[Any]:
        """Autocommit connection as ``cairn_ingest`` (needs CAIRN_INGEST_DB_PASSWORD)."""
        if self.ingest_dsn is None:
            raise RuntimeError("CAIRN_INGEST_DB_PASSWORD is not set; run `make env-update`")
        return psycopg.connect(self.ingest_dsn, autocommit=True)

    def connect_owner(self) -> psycopg.Connection[Any]:
        """Autocommit connection as ``cairn_owner``."""
        return psycopg.connect(self.owner_dsn, autocommit=True)

    def connect_app(self) -> psycopg.Connection[Any]:
        """Autocommit connection as ``cairn_app``."""
        return psycopg.connect(self.app_dsn, autocommit=True)

    def connect_admin(self) -> psycopg.Connection[Any]:
        """Autocommit connection as the test admin: the superuser, or ``cairn_admin`` in
        managed mode."""
        return psycopg.connect(self.admin_dsn, autocommit=True)

    connect_superuser = connect_admin
    """Alias kept for the tests written before managed mode."""


TEST_ORG_ID = 1
"""Test databases run in the seeded organisation, as every test did before tenancy."""


def _dsn(config: DbTestConfig, name: str, user: str, password: str) -> str:
    return make_conninfo(
        options=f"-c cairn.org_id={TEST_ORG_ID}",
        host=config.target.host,
        port=config.target.port,
        dbname=name,
        user=user,
        password=password,
        connect_timeout=CONNECT_TIMEOUT_S,
        application_name="cairn-tests",
        **libpq_tls(config.settings),
    )


def drop_database(
    admin_conn: psycopg.Connection[Any], name: str, *, managed: bool | None = None
) -> None:
    """``DROP DATABASE … WITH (FORCE)``, for ``cairn_test_*`` databases only.

    A superuser drops directly. In managed mode the admin does not own the database, so it
    runs ``SET ROLE <owner>`` first (granted by bootstrap, FV4 row 12) and always resets the
    role. ``managed`` says which; ``None`` asks the server whether the admin is a superuser.
    """
    if not name.startswith(TEST_DB_PREFIX):
        raise ValueError(f"refusing to drop {name!r}: not a {TEST_DB_PREFIX}* database")
    if managed is None:
        managed = not read_admin_facts(admin_conn).is_superuser
    drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    if not managed:
        admin_conn.execute(drop)
        return
    row = admin_conn.execute(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (name,)
    ).fetchone()
    if row is None:
        return  # nothing to drop
    admin_conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(str(row[0]))))
    try:
        admin_conn.execute(drop)
    finally:
        admin_conn.execute("RESET ROLE")


@contextmanager
def temporary_database(
    admin_conn: psycopg.Connection[Any],
    config: DbTestConfig,
    *,
    apply_migrations: bool = True,
    migrations: Sequence[Migration] | None = None,
    extensions: tuple[str, ...] = REQUIRED_EXTENSIONS,
) -> Iterator[FreshDb]:
    """Create ``cairn_test_<8 hex>`` owned by ``cairn_owner``, bootstrap it, optionally migrate
    it as the owner, and drop it on exit (also when setup or the body fails)."""
    name = TEST_DB_PREFIX + secrets.token_hex(4)
    settings = config.settings
    db = FreshDb(
        name=name,
        host=config.target.host,
        port=config.target.port,
        owner_dsn=_dsn(config, name, OWNER_ROLE, settings.owner_db_password.get_secret_value()),
        app_dsn=_dsn(config, name, "cairn_app", settings.app_db_password.get_secret_value()),
        admin_dsn=_dsn(config, name, config.target.user, config.target.password.get_secret_value()),
        ingest_dsn=(
            _dsn(config, name, INGEST_ROLE, settings.ingest_db_password.get_secret_value())
            if settings.ingest_db_password is not None
            else None
        ),
        edge_dsn=(
            _dsn(config, name, EDGE_ROLE, settings.edge_db_password.get_secret_value())
            if settings.edge_db_password is not None
            else None
        ),
    )
    connect_roles = (
        (OWNER_ROLE, APP_ROLE)
        + ((INGEST_ROLE,) if db.ingest_dsn else ())
        + ((EDGE_ROLE,) if db.edge_dsn else ())
    )
    try:
        bootstrap_database(
            admin_conn,
            name,
            spec=content_spec(name, connect_roles=connect_roles, extensions=extensions),
        )
        if apply_migrations:
            with db.connect_owner() as owner:
                migrate(owner, migrations if migrations is not None else bundled_migrations())
        yield db
    finally:
        drop_database(admin_conn, name, managed=config.target.mode == "managed")


def tables_lacking_forced_rls(conn: psycopg.Connection[Any]) -> list[str]:
    """Tables in schema ``cairn`` without both ``relrowsecurity`` and ``relforcerowsecurity``
    (Requirement 17.5). Plain and partitioned tables are checked."""
    rows = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'cairn' AND c.relkind IN ('r', 'p') "
        "AND NOT (c.relrowsecurity AND c.relforcerowsecurity) ORDER BY c.relname"
    ).fetchall()
    return [str(r[0]) for r in rows]


# --- pytest fixtures ------------------------------------------------------------------


@pytest.fixture(scope="session")
def admin_config() -> DbTestConfig:
    """Settings for the test database. Skips the requesting tests if they are incomplete."""
    try:
        return load_config()
    except DatabaseUnavailable as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def admin_conn(admin_config: DbTestConfig) -> Iterator[psycopg.Connection[Any]]:
    """Superuser connection to the compose db; runs the cluster-level bootstrap once.

    Skips with a clear message if the db is not up."""
    try:
        conn = connect_admin(admin_config)
    except DatabaseUnavailable as exc:
        pytest.skip(str(exc))
    try:
        bootstrap(conn, admin_config.settings)
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def fresh_db(admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig) -> Iterator[FreshDb]:
    """A bootstrapped and migrated ``cairn_test_<8 hex>`` database for the whole session."""
    with temporary_database(admin_conn, admin_config) as db:
        yield db
