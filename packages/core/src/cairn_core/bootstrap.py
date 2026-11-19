"""Idempotent database bootstrap (design §5, requirements 5.1 to 5.8; free-tier design §2).

``bootstrap`` runs as the admin role and creates or re-asserts, on every run:

1. the roles ``cairn_owner``, ``cairn_app`` and ``cairn_dbos`` with exact attributes
   (LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS; ``cairn_app``
   also NOINHERIT) and password verifiers;
2. the databases ``cairn`` (owner ``cairn_owner``) and ``cairn_dbos`` (owner
   ``cairn_dbos``), with CONNECT and TEMPORARY revoked from PUBLIC and CONNECT granted
   to the intended roles only;
3. inside the content database: the extensions ``vector`` and ``btree_gist``
   (retarget design FT-03) and no CREATE on schema ``public`` for PUBLIC.

Two admin modes (FT-06, FT-07), decided before anything changes (``resolve_mode``):

- ``superuser``: the admin is a superuser (local ``db``, CI). Every statement runs as the admin.
- ``managed``: the admin is a provider owner role, CREATEROLE and CREATEDB but not a
  superuser (Neon's ``neondb_owner``, the ``cairn_admin`` emulation). ALTER ROLE leaves out the
  SUPERUSER, REPLICATION and BYPASSRLS clauses (a non-superuser may not name them; roles that
  carry them are refused), the admin grants itself SET on ``cairn_owner`` and ``cairn_dbos``
  (INHERIT FALSE), database privileges and the ``public`` revoke run under ``SET ROLE <owner>``
  (a non-owner REVOKE or GRANT is a silent no-op, FV4), and the extensions are created by the
  admin itself, not under ``SET ROLE``: pgvector is not a trusted extension (FV3 item 7).

It refuses to run (before changing anything) if ``cairn_app`` or ``cairn_dbos`` is a
member of any role, or, in managed mode, if a managed role has a forbidden attribute.

The statement builders are pure: they take names, flags and password verifiers and
return ``psycopg.sql`` objects. The functions that talk to the database only read the
catalog and then execute what the builders return.

Passwords: by default a plaintext password is turned into a SCRAM-SHA-256 verifier before it
reaches any statement (``ScramEncryptor``: libpq ``PQencryptPasswordConn`` through
psycopg, with a pure-Python fallback). The one exception (OQ-1, decided 2 Nov 2026) is
managed mode with ``--allow-plaintext-password`` over ``sslmode=verify-full``: Neon accepts no
verifier, so the role statements then carry the plaintext password (``password_form``). Those
statements are never logged, echoed, returned or put into an exception message. Log fields
hold role and database names only.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, NamedTuple

import psycopg
from psycopg import sql

from cairn_core.config import AdminModeSetting, AdminSettings, EmulationSettings, SslMode

logger = logging.getLogger(__name__)

OWNER_ROLE: Final = "cairn_owner"
APP_ROLE: Final = "cairn_app"
DBOS_ROLE: Final = "cairn_dbos"
INGEST_ROLE: Final = "cairn_ingest"
EDGE_ROLE: Final = "cairn_edge"
MANAGED_ROLES: Final = (OWNER_ROLE, APP_ROLE, DBOS_ROLE)
"""The three roles bootstrap always manages (PD-06)."""

ALL_ROLES: Final = (*MANAGED_ROLES, INGEST_ROLE, EDGE_ROLE)
"""The managed roles plus ``cairn_ingest`` (Phase 1, PD1-04) and ``cairn_edge`` (Phase 3, plan 3.4).
Each is created when its password is set (``CAIRN_INGEST_DB_PASSWORD``, ``CAIRN_EDGE_DB_PASSWORD``);
every catalog check looks at all five."""

RUNTIME_ROLES: Final = (APP_ROLE, DBOS_ROLE, INGEST_ROLE, EDGE_ROLE)
"""Roles that must be members of no role."""

DBOS_DATABASE: Final = "cairn_dbos"
"""Name of the DBOS system database (PD-07). Not configurable."""

REQUIRED_EXTENSIONS: Final = ("vector", "btree_gist")
"""Extensions every content, test and spike database needs (retarget FT-03)."""

MIN_EXTENSION_VERSIONS: Final[Mapping[str, tuple[int, ...]]] = {"vector": (0, 8, 0)}
"""Lowest accepted version per extension; readiness reports ``extensions_outdated`` below it.
pgvector 0.8.0 brought iterative index scans."""

ADMIN_ROLE_EMULATED: Final = "cairn_admin"
"""The non-superuser role that stands in for Neon's ``neondb_owner`` in ``ft-db`` (FT-08)."""

PROVIDER_OWNER_ROLE: Final = "neondb_owner"
"""A server that has this role is a real Neon project: the emulation refuses to touch it."""

EMULATION_ENVIRONMENTS: Final = ("local", "ci")
"""``CAIRN_ENV`` values ``emulate-managed-admin`` runs in (Requirement 5.11)."""

TEMPLATE_DATABASE: Final = "template1"
"""Where the emulation pre-creates the extensions, so every later database has them."""

ROLE_ATTRIBUTES: Final = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
"""Attributes every managed role has (requirement 5.1)."""

# Roles that must not inherit privileges of anything they are granted (design §5).
_NOINHERIT_ROLES: Final = frozenset({APP_ROLE, INGEST_ROLE, EDGE_ROLE})

_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SCRAM_RE: Final = re.compile(
    r"^SCRAM-SHA-256\$[0-9]+:[A-Za-z0-9+/]+={0,2}\$[A-Za-z0-9+/]+={0,2}:[A-Za-z0-9+/]+={0,2}$"
)

AdminMode = Literal["superuser", "managed"]
"""How the admin role relates to the server: a superuser, or a provider owner role."""

PasswordForm = Literal["verifier", "plaintext"]
"""What goes after ``PASSWORD`` in a role statement."""

Encryptor = Callable[[str, str], str]
"""``(password, role_name) -> SCRAM-SHA-256 verifier``."""

ConnectFn = Callable[[str], "psycopg.Connection[Any]"]
"""Opens an autocommit superuser connection to the named database."""


class BootstrapError(Exception):
    """Bootstrap refused or failed. Messages name roles and databases, never a password."""


class PlaintextRefused(BootstrapError):
    """``--allow-plaintext-password`` was given where it is not accepted (a usage error, exit 2)."""


# --- password verifiers ------------------------------------------------------------------


def scram_sha256_verifier(
    password: str, *, salt: bytes | None = None, iterations: int = 4096
) -> str:
    """Pure-Python SCRAM-SHA-256 verifier in PostgreSQL's format (RFC 5802 / 7677).

    Fallback for when libpq password encryption is unavailable. Only ASCII passwords are
    supported: PostgreSQL applies SASLprep first, which this does not reproduce.
    """
    if not password.isascii():
        raise BootstrapError("a non-ASCII password needs libpq password encryption")
    salt_bytes = secrets.token_bytes(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt_bytes)}${b64(stored_key)}:{b64(server_key)}"


def is_scram_verifier(value: str) -> bool:
    """True if ``value`` looks like a SCRAM-SHA-256 verifier (never a plaintext password)."""
    return _SCRAM_RE.fullmatch(value) is not None


class ScramEncryptor:
    """Computes SCRAM-SHA-256 verifiers, preferring libpq over a live connection.

    libpq's ``PQencryptPasswordConn`` with an explicit algorithm does not query the
    server, so this works on any ``psycopg.Connection`` (Verify V4). If the connection
    has no usable ``pgconn.encrypt_password`` (an old libpq, a stub), the pure-Python
    verifier is used and ``method`` says so.
    """

    def __init__(self, conn: object | None = None) -> None:
        self._pgconn = getattr(conn, "pgconn", None)
        self.method: Literal["libpq", "python", "unused"] = "unused"

    def __call__(self, password: str, role: str) -> str:
        encrypt = getattr(self._pgconn, "encrypt_password", None)
        if encrypt is not None:
            try:
                raw = encrypt(password.encode(), role.encode(), b"scram-sha-256")
            except (AttributeError, NotImplementedError, psycopg.Error):
                raw = None
            if raw is not None:
                self.method = "libpq"
                return bytes(raw).decode("ascii")
        self.method = "python"
        return scram_sha256_verifier(password)


# --- pure statement builders -------------------------------------------------------------


def _check_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise BootstrapError(f"invalid identifier {name!r}")
    return name


@dataclass(frozen=True)
class AdminFacts:
    """What the admin connection is allowed to do (read from ``pg_roles``)."""

    user: str
    is_superuser: bool
    can_create_role: bool
    can_create_db: bool
    bypasses_rls: bool


def resolve_mode(requested: AdminModeSetting, facts: AdminFacts) -> AdminMode:
    """Decide the bootstrap mode before anything changes (Requirements 5.1 to 5.3).

    ``auto``: ``superuser`` if the admin is a superuser, else ``managed``. ``superuser`` on a
    non-superuser raises ``BootstrapError``. ``managed`` (chosen or auto-detected) needs the
    admin to have CREATEROLE and CREATEDB; the message names the missing attribute. A
    superuser may be forced into ``managed`` mode (the managed code path then runs on it).
    """
    if requested == "superuser":
        if not facts.is_superuser:
            raise BootstrapError(
                f"admin mode superuser needs a superuser, but {facts.user} is not one"
            )
        return "superuser"
    if requested == "auto" and facts.is_superuser:
        return "superuser"
    missing = [
        attribute
        for attribute, held in (
            ("CREATEROLE", facts.can_create_role),
            ("CREATEDB", facts.can_create_db),
        )
        if not held
    ]
    if missing:
        raise BootstrapError(
            f"admin mode managed needs CREATEROLE and CREATEDB, but {facts.user} lacks "
            + " and ".join(missing)
        )
    return "managed"


def plaintext_flag_problems(requested: AdminModeSetting, sslmode: SslMode) -> list[str]:
    """Why ``--allow-plaintext-password`` is refused for these settings (empty: accepted).

    The CLI asks this before it connects (exit 2), so the mode is the requested one:
    ``CAIRN_DB_ADMIN_MODE`` must be ``managed`` itself, and ``auto`` is not enough. The messages
    name keys and the required value, never anything the operator typed.
    """
    problems: list[str] = []
    if requested != "managed":
        problems.append("CAIRN_DB_ADMIN_MODE must be managed")
    if sslmode != "verify-full":
        problems.append("CAIRN_DB_SSLMODE must be verify-full")
    return problems


def password_form(mode: AdminMode, *, allow_plaintext: bool, sslmode: SslMode) -> PasswordForm:
    """OQ-1 (decided 2 Nov 2026): ``verifier`` unless ``allow_plaintext``.

    Plaintext needs managed mode and ``sslmode=verify-full``; any other combination raises
    ``PlaintextRefused`` before any connection or change. Superuser mode always sends verifiers.
    """
    if not allow_plaintext:
        return "verifier"
    problems = []
    if mode != "managed":
        problems.append("admin mode is not managed")
    if sslmode != "verify-full":
        problems.append("CAIRN_DB_SSLMODE is not verify-full")
    if problems:
        raise PlaintextRefused("--allow-plaintext-password refused: " + "; ".join(problems))
    return "plaintext"


def role_statement(
    name: str,
    secret: str,
    *,
    exists: bool,
    mode: AdminMode = "superuser",
    form: PasswordForm = "verifier",
) -> sql.Composed:
    """``CREATE ROLE`` or ``ALTER ROLE`` with the exact attributes and a password.

    ``form="verifier"`` (default): ``secret`` must be a SCRAM-SHA-256 verifier; anything else
    raises ``BootstrapError`` without echoing it, so a plaintext password cannot reach a
    statement by accident. ``form="plaintext"`` (managed mode only, OQ-1): ``secret`` is the
    password itself and is the one value that ends up in the statement text.

    CREATE always names every attribute. ALTER in superuser mode does too. ALTER in managed mode
    names ``LOGIN NOCREATEDB NOCREATEROLE [NO]INHERIT PASSWORD`` only: a non-superuser may not
    name SUPERUSER or REPLICATION (FV4), and ``role_attribute_violations`` checks those instead.
    """
    _check_name(name)
    if form == "verifier":
        if not is_scram_verifier(secret):
            raise BootstrapError(f"password for role {name} is not a SCRAM-SHA-256 verifier")
    else:
        if mode != "managed":
            raise PlaintextRefused(f"plaintext password for role {name} needs managed mode")
        if not secret or is_scram_verifier(secret):
            raise BootstrapError(f"password for role {name} is empty or already a verifier")
    inherit = "NOINHERIT" if name in _NOINHERIT_ROLES else "INHERIT"
    attrs = ROLE_ATTRIBUTES
    if exists and mode == "managed":
        attrs = "LOGIN NOCREATEDB NOCREATEROLE"
    return sql.SQL("{verb} ROLE {role} WITH {attrs} {inherit} PASSWORD {secret}").format(
        verb=sql.SQL("ALTER" if exists else "CREATE"),
        role=sql.Identifier(name),
        attrs=sql.SQL(attrs),
        inherit=sql.SQL(inherit),
        secret=sql.Literal(secret),
    )


def role_statements(
    passwords: Mapping[str, str],
    existing: Collection[str],
    encrypt: Encryptor | None,
    *,
    mode: AdminMode = "superuser",
    form: PasswordForm = "verifier",
) -> list[sql.Composed]:
    """One statement per managed role, in a fixed order.

    ``passwords`` maps every managed role to its plaintext password; ``existing`` holds
    the roles already in the cluster; ``encrypt`` turns a password into a verifier (not used,
    and may be ``None``, for ``form="plaintext"``).
    """
    missing = [r for r in MANAGED_ROLES if r not in passwords]
    if missing:
        raise BootstrapError(f"no password given for role(s): {', '.join(missing)}")
    if form == "verifier" and encrypt is None:
        raise BootstrapError("an encryptor is needed to build password verifiers")
    statements: list[sql.Composed] = []
    for role in MANAGED_ROLES:
        password = passwords[role]
        if not password:
            raise BootstrapError(f"empty password for role {role}")
        secret = password if form == "plaintext" or encrypt is None else encrypt(password, role)
        statements.append(
            role_statement(role, secret, exists=role in existing, mode=mode, form=form)
        )
    return statements


class RoleRow(NamedTuple):
    """The three attributes managed mode cannot set; read from ``pg_roles``."""

    name: str
    superuser: bool
    replication: bool
    bypassrls: bool


def role_attribute_violations(rows: Iterable[RoleRow]) -> list[str]:
    """Messages for managed roles that hold SUPERUSER, REPLICATION or BYPASSRLS.

    A non-superuser admin cannot clear those attributes, so managed mode refuses (before
    changing anything) when the list is not empty (Requirement 5.6). Names role and attribute.
    """
    found: list[str] = []
    for row in sorted(rows, key=lambda r: r.name):
        if row.name not in ALL_ROLES:
            continue
        for attribute, held in (
            ("SUPERUSER", row.superuser),
            ("REPLICATION", row.replication),
            ("BYPASSRLS", row.bypassrls),
        ):
            if held:
                found.append(f"{row.name} has {attribute}")
    return found


def admin_set_grants(admin_user: str) -> list[sql.Composed]:
    """Managed mode: ``GRANT cairn_owner TO <admin> WITH INHERIT FALSE, SET TRUE`` and the same
    for ``cairn_dbos`` (Requirement 5.7). Lets the admin create, re-own and drop databases owned
    by those roles and ``SET ROLE`` to them. The admin gains no privilege through them
    (INHERIT FALSE). Running a grant twice is a notice, not an error."""
    if not admin_user:
        raise BootstrapError("the admin role name is empty")
    return [
        sql.SQL("GRANT {role} TO {admin} WITH INHERIT FALSE, SET TRUE").format(
            role=sql.Identifier(role), admin=sql.Identifier(admin_user)
        )
        for role in (OWNER_ROLE, DBOS_ROLE)
    ]


def membership_violations(rows: Iterable[tuple[str, str]]) -> list[str]:
    """Messages for ``(member, granted_role)`` pairs where a runtime role is a member.

    Requirement 5.6: ``cairn_app`` and ``cairn_dbos`` must be members of no role.
    """
    runtime = set(RUNTIME_ROLES)
    return sorted(
        f"{member} is a member of {granted}" for member, granted in rows if member in runtime
    )


@dataclass(frozen=True)
class DatabaseSpec:
    """A database bootstrap creates: its owner, who may connect, which extensions it needs."""

    name: str
    owner: str
    connect_roles: tuple[str, ...]
    extensions: tuple[str, ...] = ()


def content_spec(
    name: str = "cairn",
    *,
    owner: str = OWNER_ROLE,
    connect_roles: tuple[str, ...] = (OWNER_ROLE, APP_ROLE),
    extensions: tuple[str, ...] = REQUIRED_EXTENSIONS,
) -> DatabaseSpec:
    """Spec for the content database and for test and spike databases (owner + app)."""
    return DatabaseSpec(name, owner, connect_roles, extensions)


def dbos_spec(name: str = DBOS_DATABASE) -> DatabaseSpec:
    """Spec for the DBOS system database: owned by and open to ``cairn_dbos`` only."""
    return DatabaseSpec(name, DBOS_ROLE, (DBOS_ROLE,), ())


def database_statements(
    spec: DatabaseSpec,
    *,
    current_owner: str | None,
    mode: AdminMode = "superuser",
    admin_user: str | None = None,
) -> list[sql.Composed]:
    """Create or re-own the database, then set its CONNECT and TEMPORARY privileges.

    ``current_owner`` is ``None`` if the database does not exist. Statements:
    ``CREATE DATABASE`` (or ``ALTER DATABASE … OWNER TO`` when the owner differs),
    ``REVOKE CONNECT, TEMPORARY … FROM PUBLIC``, ``REVOKE CONNECT`` from managed roles
    that must not connect, and ``GRANT CONNECT`` to the intended roles (requirement 5.4).

    Managed mode (``admin_user`` required): the admin is not the owner and holds no privilege
    through its SET-only membership, so the REVOKE and GRANT statements would silently do
    nothing (FV4). They run between ``SET ROLE <owner>`` and ``RESET ROLE``. The same block
    gives the admin CONNECT on the database, which it keeps (it must connect to create the
    extensions and run the ``public`` revoke, and the managed-mode role test connects as it),
    and, if the database needs extensions, a CREATE on the database that
    ``admin_access_revoke_statements`` removes again (needed by ``btree_gist``, FV4).
    """
    _check_name(spec.name)
    _check_name(spec.owner)
    db = sql.Identifier(spec.name)
    owner = sql.Identifier(spec.owner)
    statements: list[sql.Composed] = []
    if current_owner is None:
        statements.append(sql.SQL("CREATE DATABASE {db} OWNER {owner}").format(db=db, owner=owner))
    elif current_owner != spec.owner:
        statements.append(
            sql.SQL("ALTER DATABASE {db} OWNER TO {owner}").format(db=db, owner=owner)
        )
    privileges = [sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {db} FROM PUBLIC").format(db=db)]
    for role in MANAGED_ROLES:
        if role not in spec.connect_roles and role != spec.owner:
            privileges.append(
                sql.SQL("REVOKE CONNECT ON DATABASE {db} FROM {role}").format(
                    db=db, role=sql.Identifier(role)
                )
            )
    for role in spec.connect_roles:
        privileges.append(
            sql.SQL("GRANT CONNECT ON DATABASE {db} TO {role}").format(
                db=db, role=sql.Identifier(_check_name(role))
            )
        )
    if mode == "superuser":
        return [*statements, *privileges]
    if not admin_user:
        raise BootstrapError("managed mode needs the admin role name")
    admin = sql.Identifier(admin_user)
    privileges.append(
        sql.SQL("GRANT CONNECT ON DATABASE {db} TO {admin}").format(db=db, admin=admin)
    )
    if spec.extensions:
        privileges.append(
            sql.SQL("GRANT CREATE ON DATABASE {db} TO {admin}").format(db=db, admin=admin)
        )
    return [
        *statements,
        sql.SQL("SET ROLE {owner}").format(owner=owner),
        *privileges,
        sql.SQL("RESET ROLE").format(),
    ]


def admin_access_revoke_statements(
    spec: DatabaseSpec, *, mode: AdminMode, admin_user: str | None
) -> list[sql.Composed]:
    """Managed mode: take back the CREATE on the database that the admin held while it created
    the extensions. Empty in superuser mode and for a database without extensions."""
    if mode == "superuser" or not spec.extensions:
        return []
    if not admin_user:
        raise BootstrapError("managed mode needs the admin role name")
    return [
        sql.SQL("SET ROLE {owner}").format(owner=sql.Identifier(_check_name(spec.owner))),
        sql.SQL("REVOKE CREATE ON DATABASE {db} FROM {admin}").format(
            db=sql.Identifier(spec.name), admin=sql.Identifier(admin_user)
        ),
        sql.SQL("RESET ROLE").format(),
    ]


def extension_statements(
    spec: DatabaseSpec, *, mode: AdminMode = "superuser"
) -> list[sql.Composed]:
    """Statements run inside the database itself: the extensions, then the ``public`` revoke.

    Superuser mode: ``CREATE EXTENSION IF NOT EXISTS …``, then ``REVOKE CREATE ON SCHEMA public
    FROM PUBLIC``, all as the admin. Managed mode: the same ``CREATE EXTENSION`` statements
    as the admin itself, without ``SET ROLE`` (pgvector is not trusted, so the database owner
    cannot create it; on Neon the admin can), then ``SET ROLE <owner>``, the revoke (the
    database owner owns ``public`` through ``pg_database_owner``; a non-owner revoke is a silent
    no-op, FV4) and ``RESET ROLE``. ``IF NOT EXISTS`` is checked before privileges, so
    extensions that ``template1`` already carries need no privilege (FV4).
    """
    statements = [
        sql.SQL("CREATE EXTENSION IF NOT EXISTS {ext}").format(ext=sql.Identifier(ext))
        for ext in spec.extensions
    ]
    revoke = sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC").format()
    if mode == "superuser":
        return [*statements, revoke]
    return [
        *statements,
        sql.SQL("SET ROLE {owner}").format(owner=sql.Identifier(_check_name(spec.owner))),
        revoke,
        sql.SQL("RESET ROLE").format(),
    ]


def emulate_managed_admin_statements(verifier: str, *, exists: bool) -> list[sql.Composed]:
    """Local emulation of Neon's owner role (FT-08, Requirement 5.11).

    The first statement creates or re-asserts ``cairn_admin`` (LOGIN NOSUPERUSER CREATEDB
    CREATEROLE NOREPLICATION BYPASSRLS) and runs in the admin database. The remaining ones
    create ``vector`` and ``btree_gist`` and run connected to ``template1``, as the image
    superuser: ``cairn_admin`` cannot create ``vector`` itself (FV4), and Neon's admin can, so
    this gives managed-mode bootstrap the same starting point.
    """
    if not is_scram_verifier(verifier):
        raise BootstrapError(f"password for role {ADMIN_ROLE_EMULATED} is not a verifier")
    role = sql.SQL(
        "{verb} ROLE {role} WITH LOGIN NOSUPERUSER CREATEDB CREATEROLE NOREPLICATION BYPASSRLS "
        "PASSWORD {verifier}"
    ).format(
        verb=sql.SQL("ALTER" if exists else "CREATE"),
        role=sql.Identifier(ADMIN_ROLE_EMULATED),
        verifier=sql.Literal(verifier),
    )
    extensions = [
        sql.SQL("CREATE EXTENSION IF NOT EXISTS {ext}").format(ext=sql.Identifier(ext))
        for ext in REQUIRED_EXTENSIONS
    ]
    return [role, *extensions]


# --- database half -----------------------------------------------------------------------


@dataclass(frozen=True)
class RoleResult:
    name: str
    created: bool


@dataclass(frozen=True)
class DatabaseResult:
    name: str
    created: bool
    owner_changed: bool
    extensions: tuple[str, ...]


@dataclass(frozen=True)
class BootstrapResult:
    roles: tuple[RoleResult, ...]
    databases: tuple[DatabaseResult, ...]
    password_method: Literal["libpq", "python", "unused"]
    mode: AdminMode = "superuser"
    password_form: PasswordForm = "verifier"  # noqa: S105


@dataclass(frozen=True)
class EmulationResult:
    """Outcome of ``emulate_managed_admin``: no secret in it."""

    role_created: bool
    extensions: tuple[str, ...]


TLS_PARAMETERS: Final = ("sslmode", "sslrootcert")
"""libpq keywords ``connect_like`` copies from the admin connection (free-tier FT-14)."""


def tls_parameters(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """The TLS keywords ``conn`` was opened with. libpq reports only non-default values, so
    ``sslmode=prefer`` (the default) is absent and stays the default on the new connection."""
    params = conn.info.get_parameters()
    return {key: params[key] for key in TLS_PARAMETERS if params.get(key)}


def connect_like(admin_conn: psycopg.Connection[Any], dbname: str) -> psycopg.Connection[Any]:
    """Open an autocommit connection to ``dbname`` with the admin connection's credentials
    and TLS settings."""
    info = admin_conn.info
    return psycopg.connect(
        host=info.host,
        port=info.port,
        user=info.user,
        password=info.password or None,
        dbname=dbname,
        connect_timeout=10,
        application_name="cairn-db",
        autocommit=True,
        **tls_parameters(admin_conn),
    )


def _require_autocommit(conn: psycopg.Connection[Any]) -> None:
    if not conn.autocommit:
        raise ValueError("bootstrap needs an autocommit connection (CREATE DATABASE)")


def _existing_roles(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(ALL_ROLES),)
    ).fetchall()
    return {str(r[0]) for r in rows}


def read_admin_facts(conn: psycopg.Connection[Any]) -> AdminFacts:
    """Read what the connected role may do. Raises ``BootstrapError`` if it cannot be read."""
    row = conn.execute(
        "SELECT r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolbypassrls "
        "FROM pg_roles r WHERE r.rolname = session_user"
    ).fetchone()
    if row is None:
        raise BootstrapError("cannot read the attributes of the admin role")
    return AdminFacts(
        user=str(row[0]),
        is_superuser=bool(row[1]),
        can_create_role=bool(row[2]),
        can_create_db=bool(row[3]),
        bypasses_rls=bool(row[4]),
    )


def _role_rows(conn: psycopg.Connection[Any]) -> list[RoleRow]:
    rows = conn.execute(
        "SELECT rolname, rolsuper, rolreplication, rolbypassrls "
        "FROM pg_roles WHERE rolname = ANY(%s)",
        (list(ALL_ROLES),),
    ).fetchall()
    return [RoleRow(str(r[0]), bool(r[1]), bool(r[2]), bool(r[3])) for r in rows]


def _membership_rows(conn: psycopg.Connection[Any]) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT m.rolname, g.rolname FROM pg_auth_members am "
        "JOIN pg_roles m ON m.oid = am.member JOIN pg_roles g ON g.oid = am.roleid "
        "WHERE m.rolname = ANY(%s)",
        (list(RUNTIME_ROLES),),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _database_owner(conn: psycopg.Connection[Any], name: str) -> str | None:
    row = conn.execute(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (name,)
    ).fetchone()
    return None if row is None else str(row[0])


def check_memberships(admin_conn: psycopg.Connection[Any]) -> None:
    """Raise ``BootstrapError`` naming every membership of ``cairn_app`` or ``cairn_dbos``."""
    violations = membership_violations(_membership_rows(admin_conn))
    if violations:
        raise BootstrapError("runtime roles must be members of no role: " + "; ".join(violations))


def _run_as_owner_block(
    conn: psycopg.Connection[Any], statements: Sequence[sql.Composed], *, managed: bool
) -> None:
    """Execute ``statements`` in order. In managed mode a failure between ``SET ROLE`` and
    ``RESET ROLE`` resets the role before the error propagates, so the session never stays
    the database owner."""
    try:
        for statement in statements:
            conn.execute(statement)
    except BaseException:
        if managed:
            with contextlib.suppress(psycopg.Error):
                conn.execute("RESET ROLE")
        raise


def bootstrap_database(
    admin_conn: psycopg.Connection[Any],
    name: str,
    *,
    spec: DatabaseSpec | None = None,
    connect: ConnectFn | None = None,
    mode: AdminMode | None = None,
    facts: AdminFacts | None = None,
) -> DatabaseResult:
    """Create (or re-assert) one database, then its extensions and the ``public`` revoke.

    Defaults to ``content_spec(name)``: owner ``cairn_owner``, CONNECT for owner and app,
    the required extensions (``vector`` and ``btree_gist``). Test and spike databases use the
    default. The roles must already exist (``bootstrap`` creates them). ``admin_conn`` must be
    an autocommit admin connection; ``connect(dbname)`` opens one to the new database
    (default: same credentials via ``connect_like``). Running it twice changes nothing.

    ``mode`` is detected from the admin role when not given (``resolve_mode("auto", …)``);
    ``facts`` saves a second catalog read when the caller already has them.
    """
    _require_autocommit(admin_conn)
    the_spec = spec if spec is not None else content_spec(name)
    if the_spec.name != name:
        raise ValueError("spec.name must equal name")
    the_facts = facts if facts is not None else read_admin_facts(admin_conn)
    the_mode = mode if mode is not None else resolve_mode("auto", the_facts)
    managed = the_mode == "managed"
    admin_user = the_facts.user
    opener = connect if connect is not None else (lambda db: connect_like(admin_conn, db))
    current_owner = _database_owner(admin_conn, name)
    _run_as_owner_block(
        admin_conn,
        database_statements(
            the_spec, current_owner=current_owner, mode=the_mode, admin_user=admin_user
        ),
        managed=managed,
    )
    logger.info(
        "bootstrap.database",
        extra={
            "database": name,
            "was_created": current_owner is None,
            "owner_changed": current_owner not in (None, the_spec.owner),
            "mode": the_mode,
        },
    )
    try:
        with opener(name) as db_conn:
            _run_as_owner_block(
                db_conn, extension_statements(the_spec, mode=the_mode), managed=managed
            )
    finally:
        _run_as_owner_block(
            admin_conn,
            admin_access_revoke_statements(the_spec, mode=the_mode, admin_user=admin_user),
            managed=managed,
        )
    return DatabaseResult(
        name=name,
        created=current_owner is None,
        owner_changed=current_owner not in (None, the_spec.owner),
        extensions=the_spec.extensions,
    )


def bootstrap(
    admin_conn: psycopg.Connection[Any],
    settings: AdminSettings,
    *,
    connect: ConnectFn | None = None,
    encrypt: Encryptor | None = None,
    allow_plaintext: bool = False,
) -> BootstrapResult:
    """Create or re-assert roles, databases, extensions and grants. Idempotent.

    ``admin_conn``: autocommit admin connection to any database (usually the admin database).
    The content database is ``settings.db_name``. Everything that can refuse does so before
    the first change: the mode (``resolve_mode`` from ``settings.admin_mode``), the password
    form (``password_form``: plaintext only with ``allow_plaintext``, managed mode and
    ``verify-full``), a runtime role that is a member of any role, and, in managed mode, a
    managed role with SUPERUSER, REPLICATION or BYPASSRLS. Raises ``BootstrapError`` (naming
    roles and attributes, never passwords).
    """
    _require_autocommit(admin_conn)
    facts = read_admin_facts(admin_conn)
    mode = resolve_mode(settings.admin_mode, facts)
    form = password_form(mode, allow_plaintext=allow_plaintext, sslmode=settings.db_sslmode)
    check_memberships(admin_conn)
    if mode == "managed":
        role_rows = _role_rows(admin_conn)
        violations = role_attribute_violations(role_rows)
        if violations:
            raise BootstrapError(
                "managed mode cannot clear role attributes: " + "; ".join(violations)
            )
        existing = {row.name for row in role_rows}
    else:
        existing = _existing_roles(admin_conn)
    encryptor = encrypt if encrypt is not None else ScramEncryptor(admin_conn)
    passwords = {
        OWNER_ROLE: settings.owner_db_password.get_secret_value(),
        APP_ROLE: settings.app_db_password.get_secret_value(),
        DBOS_ROLE: settings.dbos_db_password.get_secret_value(),
    }
    statements = role_statements(
        passwords, existing, encryptor if form == "verifier" else None, mode=mode, form=form
    )
    logger.info("bootstrap.mode", extra={"mode": mode, "credential_form": form})
    role_results: list[RoleResult] = []
    for role, statement in zip(MANAGED_ROLES, statements, strict=True):
        admin_conn.execute(statement)
        created = role not in existing
        role_results.append(RoleResult(role, created))
        logger.info("bootstrap.role", extra={"role": role, "was_created": created})
    ingest_password = (
        settings.ingest_db_password.get_secret_value()
        if settings.ingest_db_password is not None
        else None
    )
    if ingest_password:
        # Phase 1 (PD1-04): the content writer. Same attributes as the other roles, NOINHERIT,
        # no memberships, CONNECT on the content database only.
        secret = ingest_password if form == "plaintext" else encryptor(ingest_password, INGEST_ROLE)
        admin_conn.execute(
            role_statement(
                INGEST_ROLE, secret, exists=INGEST_ROLE in existing, mode=mode, form=form
            )
        )
        role_results.append(RoleResult(INGEST_ROLE, INGEST_ROLE not in existing))
        logger.info(
            "bootstrap.role",
            extra={"role": INGEST_ROLE, "was_created": INGEST_ROLE not in existing},
        )
    edge_password = (
        settings.edge_db_password.get_secret_value()
        if settings.edge_db_password is not None
        else None
    )
    if edge_password:
        # Phase 3 (plan 3.4): the webhook receiver at the edge. It may insert thin rows into the
        # sync inbox and nothing else (migration 0010); same attributes, NOINHERIT, no memberships.
        secret = edge_password if form == "plaintext" else encryptor(edge_password, EDGE_ROLE)
        admin_conn.execute(
            role_statement(EDGE_ROLE, secret, exists=EDGE_ROLE in existing, mode=mode, form=form)
        )
        role_results.append(RoleResult(EDGE_ROLE, EDGE_ROLE not in existing))
        logger.info(
            "bootstrap.role", extra={"role": EDGE_ROLE, "was_created": EDGE_ROLE not in existing}
        )
    if mode == "managed":
        for grant in admin_set_grants(facts.user):
            admin_conn.execute(grant)
    content_roles = (
        OWNER_ROLE,
        APP_ROLE,
        *((INGEST_ROLE,) if ingest_password else ()),
        *((EDGE_ROLE,) if edge_password else ()),
    )
    databases = (
        bootstrap_database(
            admin_conn,
            settings.db_name,
            spec=content_spec(settings.db_name, connect_roles=content_roles),
            connect=connect,
            mode=mode,
            facts=facts,
        ),
        bootstrap_database(
            admin_conn, DBOS_DATABASE, spec=dbos_spec(), connect=connect, mode=mode, facts=facts
        ),
    )
    method = encryptor.method if isinstance(encryptor, ScramEncryptor) else "unused"
    if form == "plaintext":
        method = "unused"
    return BootstrapResult(tuple(role_results), databases, method, mode, form)


def emulation_refusals(settings: EmulationSettings) -> list[str]:
    """Why ``emulate-managed-admin`` must not run here, from the settings alone (Requirement 5.12).
    Checked before connecting."""
    if settings.env not in EMULATION_ENVIRONMENTS:
        return [f"CAIRN_ENV must be {' or '.join(EMULATION_ENVIRONMENTS)}"]
    return []


def emulate_managed_admin(
    admin_conn: psycopg.Connection[Any],
    settings: EmulationSettings,
    *,
    connect: ConnectFn | None = None,
    encrypt: Encryptor | None = None,
) -> EmulationResult:
    """Create or re-assert ``cairn_admin`` and pre-create the extensions in ``template1``.

    Refuses (``BootstrapError``, nothing changed) when ``CAIRN_ENV`` is not ``local`` or ``ci``,
    when the server has a role named ``neondb_owner`` (a real Neon project), or when the
    connected role is not a superuser. Idempotent. The password comes from
    ``settings.managed_admin_password`` and is turned into a verifier before use.
    """
    _require_autocommit(admin_conn)
    refusals = emulation_refusals(settings)
    if refusals:
        raise BootstrapError("emulate-managed-admin refused: " + "; ".join(refusals))
    if admin_conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (PROVIDER_OWNER_ROLE,)
    ).fetchone():
        raise BootstrapError(
            f"emulate-managed-admin refused: the server has a role named {PROVIDER_OWNER_ROLE}"
        )
    resolve_mode("superuser", read_admin_facts(admin_conn))
    encryptor = encrypt if encrypt is not None else ScramEncryptor(admin_conn)
    exists = (
        admin_conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (ADMIN_ROLE_EMULATED,)
        ).fetchone()
        is not None
    )
    role_stmt, *extension_stmts = emulate_managed_admin_statements(
        encryptor(settings.managed_admin_password.get_secret_value(), ADMIN_ROLE_EMULATED),
        exists=exists,
    )
    admin_conn.execute(role_stmt)
    logger.info(
        "bootstrap.emulation", extra={"role": ADMIN_ROLE_EMULATED, "was_created": not exists}
    )
    opener = connect if connect is not None else (lambda db: connect_like(admin_conn, db))
    with opener(TEMPLATE_DATABASE) as template_conn:
        for statement in extension_stmts:
            template_conn.execute(statement)
    return EmulationResult(role_created=not exists, extensions=REQUIRED_EXTENSIONS)


def describe(result: BootstrapResult) -> Sequence[str]:
    """Stable output lines for ``cairn-db bootstrap`` (no secrets in any of them)."""
    lines = [f"admin mode: {result.mode}"]
    lines += [f"role {r.name}: {'created' if r.created else 'updated'}" for r in result.roles]
    for d in result.databases:
        state = "created" if d.created else ("owner changed" if d.owner_changed else "exists")
        lines.append(f"database {d.name}: {state}")
        if d.extensions:
            lines.append(f"extensions {d.name}: {', '.join(sorted(d.extensions))}")
    if result.password_form == "plaintext":  # noqa: S105
        lines.append(
            "role passwords sent as plaintext over verify-full TLS (--allow-plaintext-password)"
        )
    lines.append(f"bootstrap complete (password encryption: {result.password_method})")
    return lines


def describe_emulation(result: EmulationResult) -> Sequence[str]:
    """Stable output lines for ``cairn-db emulate-managed-admin`` (no secrets)."""
    return [
        f"role {ADMIN_ROLE_EMULATED}: {'created' if result.role_created else 'updated'}",
        f"extensions {TEMPLATE_DATABASE}: {', '.join(sorted(result.extensions))}",
        "emulation complete",
    ]
