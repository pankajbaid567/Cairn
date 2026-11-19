"""``cairn-db``: database administration commands (design §5 and §6).

Commands here:

- ``cairn-db bootstrap``: create roles, databases and extensions as the admin
  (``CAIRN_DB_ADMIN_USER`` in ``CAIRN_DB_ADMIN_DATABASE``, default ``postgres`` in ``postgres``).
- ``cairn-db migrate``: apply pending migrations as the owner role.
- ``cairn-db init``: ``bootstrap``, then ``migrate`` (what the compose ``db-init`` runs).
- ``cairn-db status``: print applied and pending migrations.

Exit codes: 0 success; 1 a migration or database error; 2 invalid configuration
(from ``load_settings``) or a usage error (from click). Output and errors name
versions and error codes only: never a password or a connection string.
"""

from __future__ import annotations

import math
from typing import Any

import click
import psycopg

from cairn_core.bootstrap import (
    BootstrapError,
    PlaintextRefused,
    describe,
    describe_emulation,
    emulate_managed_admin,
    emulation_refusals,
    plaintext_flag_problems,
)
from cairn_core.bootstrap import bootstrap as run_bootstrap
from cairn_core.config import (
    ADMIN_PASSWORD_KEYS,
    CONFIG_EXIT_STATUS,
    AdminConnectionSettings,
    AdminSettings,
    CommonSettings,
    EmulationSettings,
    libpq_tls,
    load_settings,
)
from cairn_core.embed_dim import EmbedDimError, ensure_embedding_dimension
from cairn_core.migrate import (
    MigrationError,
    bundled_migrations,
    head,
    migrate,
    read_applied,
    status,
)
from cairn_core.telemetry import configure_logging

OWNER_ROLE = "cairn_owner"
"""Role that owns the content database and runs migrations (PD-06). Not configurable."""


def connect_as(
    settings: CommonSettings,
    *,
    user: str,
    password: str,
    dbname: str | None = None,
) -> psycopg.Connection[Any]:
    """Open an autocommit connection. Parameters are passed as keywords, so no
    connection string with a password is ever built. TLS comes from ``libpq_tls``."""
    return psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=dbname or settings.db_name,
        user=user,
        password=password,
        connect_timeout=max(1, math.ceil(settings.db_timeout_s)),
        application_name="cairn-db",
        autocommit=True,
        **libpq_tls(settings),
    )


def _connect_owner(settings: AdminSettings) -> psycopg.Connection[Any]:
    return connect_as(
        settings, user=OWNER_ROLE, password=settings.owner_db_password.get_secret_value()
    )


def _connect_admin(settings: AdminConnectionSettings) -> psycopg.Connection[Any]:
    """Connect as ``CAIRN_DB_ADMIN_USER`` to ``CAIRN_DB_ADMIN_DATABASE`` (free-tier §6)."""
    password = settings.admin_password
    if password is None:
        # Name the keys only, in the same shape as load_settings.
        keys = " or ".join(ADMIN_PASSWORD_KEYS)
        click.echo(f"invalid configuration: {keys} (missing)", err=True)
        raise SystemExit(CONFIG_EXIT_STATUS)
    return connect_as(
        settings,
        user=settings.admin_user,
        password=password.get_secret_value(),
        dbname=settings.admin_database,
    )


def _fail(message: str) -> SystemExit:
    click.echo(message, err=True)
    return SystemExit(1)


def _database_error(exc: psycopg.Error) -> SystemExit:
    # Class and SQLSTATE only: connection errors can carry host details.
    state = f" ({exc.sqlstate})" if exc.sqlstate else ""
    return _fail(f"database error: {type(exc).__name__}{state}")


@click.group()
def main() -> None:
    """Cairn database administration."""


ALLOW_PLAINTEXT_HELP = (
    "Send role passwords as plaintext instead of SCRAM verifiers (Neon accepts no verifier). "
    "Needs CAIRN_DB_ADMIN_MODE=managed and CAIRN_DB_SSLMODE=verify-full, otherwise exit 2 "
    "before connecting."
)


def _check_plaintext_flag(settings: AdminSettings, allow_plaintext: bool) -> None:
    """Refuse the flag (exit 2) before any connection when its conditions do not hold."""
    if not allow_plaintext:
        return
    problems = plaintext_flag_problems(settings.admin_mode, settings.db_sslmode)
    if problems:
        click.echo(f"--allow-plaintext-password refused: {'; '.join(problems)}", err=True)
        raise SystemExit(CONFIG_EXIT_STATUS)


def _bootstrap(settings: AdminSettings, *, allow_plaintext: bool = False) -> None:
    _check_plaintext_flag(settings, allow_plaintext)
    try:
        with _connect_admin(settings) as conn:
            result = run_bootstrap(conn, settings, allow_plaintext=allow_plaintext)
    except PlaintextRefused as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_EXIT_STATUS) from exc
    except BootstrapError as exc:
        raise _fail(f"bootstrap failed: {exc}") from exc
    except psycopg.Error as exc:
        raise _database_error(exc) from exc
    for line in describe(result):
        click.echo(line)


def _migrate(settings: AdminSettings) -> None:
    migrations = bundled_migrations()
    by_version = {m.version: m for m in migrations}
    try:
        with _connect_owner(settings) as conn:
            done = migrate(conn, migrations)
    except MigrationError as exc:
        raise _fail(f"migration failed: {exc}") from exc
    except psycopg.Error as exc:
        raise _database_error(exc) from exc
    for version in done:
        click.echo(f"applied {by_version[version].filename}")
    if done:
        click.echo(f"applied {len(done)} migration(s); now at version {head(migrations)}")
    else:
        click.echo(f"up to date at version {head(migrations)}; nothing applied")


@main.command("bootstrap")
@click.option("--allow-plaintext-password", is_flag=True, help=ALLOW_PLAINTEXT_HELP)
def bootstrap_command(allow_plaintext_password: bool) -> None:
    """Create roles, databases and extensions as the admin role. Safe to run again."""
    settings = load_settings(AdminSettings)
    configure_logging("cairn-db", settings.log_level)
    _bootstrap(settings, allow_plaintext=allow_plaintext_password)


@main.command("migrate")
def migrate_command() -> None:
    """Apply pending migrations as cairn_owner. Applying nothing is success."""
    settings = load_settings(AdminSettings)
    configure_logging("cairn-db", settings.log_level)
    _migrate(settings)


EMBED_DIM_HELP = (
    "Make the embedding column a vector of this dimension (256, 512 or 1024 for Titan). "
    "Default: keep the dimension the migrations created (384)."
)


def _set_embed_dim(settings: AdminSettings, dim: int, *, reset: bool) -> None:
    try:
        with _connect_owner(settings) as conn:
            click.echo(ensure_embedding_dimension(conn, dim, reset=reset))
    except EmbedDimError as exc:
        raise _fail(f"embedding dimension: {exc}") from exc
    except psycopg.Error as exc:
        raise _database_error(exc) from exc


@main.command("init")
@click.option("--allow-plaintext-password", is_flag=True, help=ALLOW_PLAINTEXT_HELP)
@click.option("--embed-dim", type=int, envvar="CAIRN_EMBED_DIM", default=None, help=EMBED_DIM_HELP)
@click.option(
    "--reset-embeddings", is_flag=True, help="Delete stored embeddings if the dimension changes."
)
def init_command(
    allow_plaintext_password: bool, embed_dim: int | None, reset_embeddings: bool
) -> None:
    """Bootstrap, migrate, then set the embedding dimension when asked. Safe to run again."""
    settings = load_settings(AdminSettings)
    configure_logging("cairn-db", settings.log_level)
    _bootstrap(settings, allow_plaintext=allow_plaintext_password)
    _migrate(settings)
    if embed_dim is not None:
        _set_embed_dim(settings, embed_dim, reset=reset_embeddings)


@main.command("set-embed-dim")
@click.argument("dim", type=int)
@click.option(
    "--reset-embeddings", is_flag=True, help="Delete stored embeddings if the dimension changes."
)
def set_embed_dim_command(dim: int, reset_embeddings: bool) -> None:
    """Change the embedding column to ``DIM`` dimensions (after migrations, before embedding)."""
    settings = load_settings(AdminSettings)
    configure_logging("cairn-db", settings.log_level)
    _set_embed_dim(settings, dim, reset=reset_embeddings)


@main.command("emulate-managed-admin")
def emulate_managed_admin_command() -> None:
    """Local and CI only: create cairn_admin and the extensions in template1 as the superuser.

    Makes a plain PostgreSQL behave like Neon for managed-mode bootstrap. Refuses with
    CAIRN_ENV=demo and on a server that has a neondb_owner role. Safe to run again.
    """
    settings = load_settings(EmulationSettings)
    configure_logging("cairn-db", settings.log_level)
    refusals = emulation_refusals(settings)
    if refusals:  # before any connection
        raise _fail(f"emulate-managed-admin refused: {'; '.join(refusals)}")
    try:
        with _connect_admin(settings) as conn:
            result = emulate_managed_admin(conn, settings)
    except BootstrapError as exc:
        raise _fail(f"emulate-managed-admin failed: {exc}") from exc
    except psycopg.Error as exc:
        raise _database_error(exc) from exc
    for line in describe_emulation(result):
        click.echo(line)


@main.command("status")
def status_command() -> None:
    """Print applied and pending migrations. Fails if the database has drifted."""
    settings = load_settings(AdminSettings)
    configure_logging("cairn-db", settings.log_level)
    migrations = bundled_migrations()
    try:
        with _connect_owner(settings) as conn:
            applied = read_applied(conn)
        report = status(migrations, applied)
    except MigrationError as exc:
        raise _fail(f"migration status: {exc}") from exc
    except psycopg.Error as exc:
        raise _database_error(exc) from exc
    click.echo(f"head: {report.head}  current: {report.current}  pending: {len(report.pending)}")
    for a in report.applied:
        stamp = a.applied_at.isoformat(timespec="seconds")
        click.echo(f"applied  {a.version:04d}  {a.name}  {a.checksum[:12]}  {stamp}")
    for m in report.pending:
        click.echo(f"pending  {m.version:04d}  {m.name}  {m.checksum[:12]}")
