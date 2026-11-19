"""The S2 procedure (Phase 0 design §14; free-tier retarget design §5, Requirement 3).

``run`` recreates a ``cairn_spike_*`` database through ``bootstrap_database``, loads an S1
dataset into ``s2.chunks``, adds the generated ``body_tsv`` column, builds the full-text
search (GIN on ``body_tsv``), HNSW and ACL GIN indexes, turns on FORCE RLS with
the ``acl_read`` policy, checks default deny, then runs every variant x principal x
iterative-scan setting as ``cairn_app`` (and once as the superuser for a baseline) under
``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)``. HNSW variants also get recall at 10 against
exact search with the same visibility.

Roles: the owner builds and grants, ``cairn_app`` runs every measured query, the
superuser runs the baseline and the drop. Nothing outside the spike database is touched.
"""

from __future__ import annotations

import re
import statistics
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import psycopg
import pyarrow.parquet as pq
from psycopg import sql
from psycopg.conninfo import make_conninfo

from cairn_core.bootstrap import (
    APP_ROLE,
    MANAGED_ROLES,
    OWNER_ROLE,
    bootstrap_database,
    tls_parameters,
)
from cairn_core.config import ADMIN_PASSWORD_KEYS, AdminSettings, libpq_tls
from cairn_eval.spikes.s1_acl_corpus import S1Config, generate, load, read_manifest
from cairn_eval.spikes.s1_acl_corpus.generate import QUERIES_FILE
from cairn_eval.spikes.s2_rls_explain.analyze import IndexNames, PlanFacts, Role, analyze_plan
from cairn_eval.spikes.s2_rls_explain.config import (
    PRINCIPAL_SETTING,
    RESULT_LIMIT,
    SPIKE_SCHEMA,
    SPIKE_TABLE,
    S2Config,
)
from cairn_eval.spikes.s2_rls_explain.queries import (
    EXPLAIN,
    TABLE,
    TS_CONFIG,
    TSV_COLUMN,
    VARIANTS,
    build_query,
    is_explicit,
    set_local,
    set_principal,
    shape_of,
)
from cairn_eval.spikes.s2_rls_explain.report import (
    DefaultDenyResults,
    LexicalIndexOutcome,
    S2Report,
    summarize,
)

INDEXES: Final = IndexNames(
    table=SPIKE_TABLE,
    fts="chunks_fts_idx",
    hnsw="chunks_embedding_hnsw_idx",
    gin="chunks_acl_gin_idx",
)
POLICY_NAME: Final = "acl_read"
NO_PRINCIPAL: Final = "n/a"
"""Principal label of a baseline run whose query has no ACL predicate."""
HNSW_M: Final = 16
HNSW_EF_CONSTRUCTION: Final = 64
GUC_NAMES: Final = (
    "shared_buffers",
    "work_mem",
    "maintenance_work_mem",
    "effective_cache_size",
    "max_parallel_workers_per_gather",
    "random_page_cost",
    "jit",
    "max_connections",
)
_FIRST_LINE: Final = re.compile(r"[^\r\n]*")

Progress = Callable[[str], None]
Execute = Callable[[sql.Composed], object]
"""Runs one statement on an autocommit connection; raises ``psycopg.Error`` if it fails."""


class HarnessError(Exception):
    """The harness cannot run (missing role, bad dataset or principal). No secret in it."""


def _describe(exc: BaseException) -> str:
    first = _FIRST_LINE.match(str(exc))
    text = first.group(0).strip() if first else ""
    state = getattr(exc, "sqlstate", None)
    suffix = f" ({state})" if state else ""
    return f"{type(exc).__name__}{suffix}: {text}" if text else f"{type(exc).__name__}{suffix}"


# --- index statements -----------------------------------------------------------------


def tsv_column_statement() -> sql.Composed:
    """``ALTER TABLE s2.chunks ADD COLUMN body_tsv tsvector GENERATED ALWAYS AS
    (to_tsvector('simple', body)) STORED`` (Requirement 3.1)."""
    return sql.SQL(
        "ALTER TABLE {table} ADD COLUMN {col} tsvector "
        "GENERATED ALWAYS AS (to_tsvector({config}, body)) STORED"
    ).format(table=TABLE, col=sql.Identifier(TSV_COLUMN), config=sql.Literal(TS_CONFIG))


def fts_statement() -> sql.Composed:
    """``CREATE INDEX chunks_fts_idx ON s2.chunks USING gin (body_tsv)``."""
    return sql.SQL("CREATE INDEX {name} ON {table} USING gin ({col})").format(
        name=sql.Identifier(INDEXES.fts), table=TABLE, col=sql.Identifier(TSV_COLUMN)
    )


def hnsw_statement() -> sql.Composed:
    return sql.SQL(
        "CREATE INDEX {name} ON {table} USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = {m}, ef_construction = {ef})"
    ).format(
        name=sql.Identifier(INDEXES.hnsw),
        table=TABLE,
        m=sql.Literal(HNSW_M),
        ef=sql.Literal(HNSW_EF_CONSTRUCTION),
    )


def gin_statement() -> sql.Composed:
    return sql.SQL("CREATE INDEX {name} ON {table} USING gin (acl)").format(
        name=sql.Identifier(INDEXES.gin), table=TABLE
    )


# --- connections ----------------------------------------------------------------------


def _connect(
    admin_conn: psycopg.Connection[Any], dbname: str, user: str, password: str | None
) -> psycopg.Connection[Any]:
    info = admin_conn.info
    return psycopg.connect(
        make_conninfo(
            host=info.host,
            port=info.port,
            dbname=dbname,
            user=user,
            password=password,
            connect_timeout=10,
            application_name="cairn-s2",
            **tls_parameters(admin_conn),  # the admin connection's TLS settings (FT-14)
        ),
        autocommit=True,
        # A statement without parameters is planned once and its plan is reused, whatever
        # the principal or hnsw settings of the next execution. psycopg prepares a query
        # text after five executions, so preparation is off: each run plans afresh.
        prepare_threshold=None,
    )


def _open_admin(admin: AdminSettings, admin_user: str | None) -> psycopg.Connection[Any]:
    if admin.admin_password is None:
        raise HarnessError(f"{' or '.join(ADMIN_PASSWORD_KEYS)} is needed for the S2 harness")
    return psycopg.connect(
        host=admin.db_host,
        port=admin.db_port,
        dbname=admin.admin_database,
        user=admin_user or admin.admin_user,
        password=admin.admin_password.get_secret_value(),
        connect_timeout=10,
        application_name="cairn-s2",
        autocommit=True,
        **libpq_tls(admin),
    )


# --- dataset --------------------------------------------------------------------------


def _read_queries(dataset: Path, wanted: int) -> list[tuple[list[float], list[str]]]:
    table = pq.read_table(dataset / QUERIES_FILE).slice(0, wanted)
    vectors = table.column("embedding").to_pylist()
    terms = table.column("topic_terms").to_pylist()
    return [(list(v or []), list(t or [])) for v, t in zip(vectors, terms, strict=True)]


def _dataset_info(dataset: Path, source: str) -> dict[str, Any]:
    manifest = read_manifest(dataset)
    return {
        "source": source,
        "rows": manifest.rows,
        "dim": manifest.dim,
        "acl_mode": manifest.config.acl_mode,
        "generator_version": manifest.generator_version,
        "content_sha256": manifest.content_sha256,
        "seed": manifest.config.seed,
        "principals": manifest.principals,
    }


# --- build ----------------------------------------------------------------------------


def _roles_exist(admin_conn: psycopg.Connection[Any]) -> bool:
    rows = admin_conn.execute(
        "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(MANAGED_ROLES),)
    ).fetchone()
    return rows is not None and rows[0] == len(MANAGED_ROLES)


def recreate_database(admin_conn: psycopg.Connection[Any], name: str) -> None:
    """Drop (if present) and bootstrap the spike database. ``S2Config`` has already
    restricted ``name`` to ``cairn_spike_*``; the check is repeated here because this
    statement is destructive."""
    if not name.startswith("cairn_spike_"):
        raise HarnessError(f"refusing to recreate {name!r}: not a cairn_spike_* database")
    admin_conn.execute(
        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    )
    bootstrap_database(admin_conn, name)


def build_spike(
    owner: psycopg.Connection[Any], dataset: Path, cfg: S2Config, progress: Progress
) -> LexicalIndexOutcome:
    """As ``cairn_owner``: schema, table (via the S1 loader), the generated ``body_tsv``
    column, indexes, ANALYZE."""
    owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SPIKE_SCHEMA)))
    progress("loading the dataset")
    load(dataset, owner, f"{SPIKE_SCHEMA}.{SPIKE_TABLE}")
    # The S1 loader names the key chunk_id; the S2 table of design §14 calls it id.
    owner.execute(sql.SQL("ALTER TABLE {} RENAME COLUMN chunk_id TO id").format(TABLE))
    # Session only: the connection closes at the end of the build.
    owner.execute(
        sql.SQL("SET maintenance_work_mem = {}").format(sql.Literal(cfg.maintenance_work_mem))
    )
    progress("adding the generated tsvector column")
    owner.execute(tsv_column_statement())
    progress("building the full-text search index")
    owner.execute(fts_statement())
    progress("building the HNSW index")
    owner.execute(hnsw_statement())
    progress("building the GIN index")
    owner.execute(gin_statement())
    owner.execute(sql.SQL("ANALYZE {}").format(TABLE))
    row = owner.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
        (SPIKE_SCHEMA, INDEXES.fts),
    ).fetchone()
    return LexicalIndexOutcome(index_definition=str(row[0]) if row else "")


def enable_rls(owner: psycopg.Connection[Any]) -> None:
    """ENABLE and FORCE RLS, the SELECT policy for ``cairn_app``, and the grants.

    The policy is design §14 step 2 with one change: the setting is passed through
    ``nullif(…, '')``. After a transaction that used ``SET LOCAL``, a connection keeps the
    setting as the empty string, and ``''::int[]`` raises instead of denying (Verify V5).
    """
    owner.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(TABLE))
    owner.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(TABLE))
    owner.execute(
        sql.SQL(
            "CREATE POLICY {policy} ON {table} FOR SELECT TO {app} "
            "USING (acl && nullif(current_setting({setting}, true), '')::int[])"
        ).format(
            policy=sql.Identifier(POLICY_NAME),
            table=TABLE,
            app=sql.Identifier(APP_ROLE),
            setting=sql.Literal(PRINCIPAL_SETTING),
        )
    )
    owner.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(SPIKE_SCHEMA), sql.Identifier(APP_ROLE)
        )
    )
    owner.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(TABLE, sql.Identifier(APP_ROLE)))


# --- default deny ---------------------------------------------------------------------


def _count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(TABLE)).fetchone()
    return int(row[0]) if row else -1


def check_default_deny(
    connect_app: Callable[[], psycopg.Connection[Any]],
    connect_owner: Callable[[], psycopg.Connection[Any]],
    connect_super: Callable[[], psycopg.Connection[Any]],
    expected_rows: int,
) -> DefaultDenyResults:
    """Requirement 16.5. Each check uses a fresh connection so no setting carries over,
    except the stale-setting check, which creates that situation on purpose."""
    with connect_app() as conn:
        no_setting = _count(conn)
    with connect_app() as conn, conn.transaction():
        conn.execute(set_principal([]))
        empty = _count(conn)
    with connect_app() as conn:
        with conn.transaction():
            conn.execute(set_principal([1000]))
        stale = _count(conn)  # the setting now reads '' on this connection
    error: str | None = None
    with connect_app() as conn:
        try:
            conn.execute("SET row_security = off")
            _count(conn)
        except psycopg.Error as exc:
            error = _describe(exc)
    with connect_owner() as conn:
        owner_rows = _count(conn)
    with connect_super() as conn:
        super_rows = _count(conn)
    return DefaultDenyResults(
        no_setting_rows=no_setting,
        empty_principal_rows=empty,
        stale_setting_rows=stale,
        row_security_off_error=error,
        owner_rows=owner_rows,
        superuser_rows=super_rows,
        expected_rows=expected_rows,
    )


# --- measurement ----------------------------------------------------------------------


def _apply_settings(
    conn: psycopg.Connection[Any], groups: Sequence[int] | None, ef_search: int, iterative: str
) -> None:
    if groups is not None:
        conn.execute(set_principal(groups))
    conn.execute(set_local("hnsw.ef_search", ef_search))
    conn.execute(set_local("hnsw.iterative_scan", iterative))


def explain_once(
    conn: psycopg.Connection[Any],
    query: sql.Composed,
    *,
    principal_groups: Sequence[int] | None,
    ef_search: int,
    iterative: str,
) -> Any:
    """One ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` in its own transaction with the
    settings applied by ``SET LOCAL``. ``principal_groups`` is ``None`` for the superuser."""
    with conn.transaction():
        _apply_settings(conn, principal_groups, ef_search, iterative)
        row = conn.execute(EXPLAIN + query).fetchone()
    if row is None:
        raise HarnessError("EXPLAIN returned no plan")
    return row[0]


def _ids(conn: psycopg.Connection[Any], query: sql.Composed) -> list[int]:
    return [int(r[0]) for r in conn.execute(query).fetchall()]


def recall_at_10(
    conn: psycopg.Connection[Any],
    variant: str,
    queries: Sequence[tuple[list[float], list[str]]],
    *,
    groups: Sequence[int],
    principal_groups: Sequence[int] | None,
    ef_search: int,
    iterative: str,
) -> float | None:
    """Mean of |approximate ∩ exact| / |exact| over ``queries``, both under the same role,
    policy and explicit predicate. Exact search turns the index scan types off, so the
    planner reads the table and sorts. ``None`` if no query has any visible row."""
    scores: list[float] = []
    for vector, terms in queries:
        query = build_query(variant, terms=terms, vector=vector, groups=groups, limit=RESULT_LIMIT)
        with conn.transaction():
            _apply_settings(conn, principal_groups, ef_search, iterative)
            approximate = set(_ids(conn, query))
            for name in ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan"):
                conn.execute(sql.SQL("SET LOCAL {} = off").format(sql.SQL(name)))
            exact = set(_ids(conn, query))
        if exact:
            scores.append(len(approximate & exact) / len(exact))
    return statistics.fmean(scores) if scores else None


def measure(
    conn: psycopg.Connection[Any],
    *,
    variant: str,
    principal: str,
    role: Role,
    iterative: str,
    groups: Sequence[int],
    principal_groups: Sequence[int] | None,
    queries: Sequence[tuple[list[float], list[str]]],
    cfg: S2Config,
) -> tuple[PlanFacts, str]:
    """``repeats`` explains of the first query; returns the median-time run's facts (with
    recall for HNSW variants) and the statement text."""
    vector, terms = queries[0]
    query = build_query(variant, terms=terms, vector=vector, groups=groups)
    runs = [
        analyze_plan(
            explain_once(
                conn,
                query,
                principal_groups=principal_groups,
                ef_search=cfg.ef_search,
                iterative=iterative,
            ),
            INDEXES,
            variant=variant,
            principal=principal,
            role=role,
            iterative_scan=iterative,
        )
        for _ in range(cfg.repeats)
    ]
    median = sorted(runs, key=lambda f: f.execution_ms)[len(runs) // 2]
    recall = None
    if shape_of(variant) == "hnsw":
        recall = recall_at_10(
            conn,
            variant,
            queries[: cfg.recall_queries],
            groups=groups,
            principal_groups=principal_groups,
            ef_search=cfg.ef_search,
            iterative=iterative,
        )
    return replace(median, recall_at_10=recall), query.as_string(conn)


def _matrix(cfg: S2Config) -> list[tuple[Role, str, str, str]]:
    """``(role, variant, principal, iterative_scan)`` for every run.

    The superuser has no policy, so a variant without the explicit predicate does not
    depend on the principal and runs once per iterative-scan setting.
    """
    runs: list[tuple[Role, str, str, str]] = []
    for variant in VARIANTS:
        for principal in cfg.principals:
            for iterative in cfg.iterative_scan:
                runs.append(("cairn_app", variant, principal, iterative))
    for variant in VARIANTS:
        principals = cfg.principals if is_explicit(variant) else (NO_PRINCIPAL,)
        for principal in principals:
            for iterative in cfg.iterative_scan:
                runs.append(("superuser_baseline", variant, principal, iterative))
    return runs


# --- environment ----------------------------------------------------------------------

_IMAGE_REF: Final = re.compile(
    r"^[^#\n]*?(?<![\w./-])(?P<ref>pgvector/pgvector:[^\s\"'#]+)", re.MULTILINE
)
"""The ``db`` image: ``pgvector/pgvector:<tag>[@sha256:<digest>]`` outside a comment,
wherever the compose file writes it (the ``x-pg-image`` anchor that ``db`` and ``ft-db``
share)."""


def find_compose_file(start: Path | None = None) -> Path | None:
    """``docker-compose.yml`` in ``start`` or the nearest parent that has one."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / "docker-compose.yml"
        if candidate.is_file():
            return candidate
    return None


def image_reference(compose_file: Path | None) -> dict[str, str]:
    """Image tag and digest of the ``db`` service as written in the compose file: the first
    ``pgvector/pgvector`` reference in it."""
    path = compose_file if compose_file is not None else find_compose_file()
    if path is None or not path.is_file():
        return {"image_tag": "unknown", "image_digest": "unknown", "image_source": "not found"}
    match = _IMAGE_REF.search(path.read_text("utf-8"))
    if match is None:
        return {"image_tag": "unknown", "image_digest": "unknown", "image_source": str(path)}
    reference = match.group("ref")
    tag, _, digest = reference.partition("@")
    return {
        "image_tag": tag,
        "image_digest": digest or "unknown",
        "image_source": f"{path.name} (service db)",
    }


def collect_environment(super_conn: psycopg.Connection[Any], cfg: S2Config) -> dict[str, Any]:
    """Versions, settings, index definitions and sizes, read from the spike database."""
    version = super_conn.execute("SELECT version()").fetchone()
    server = super_conn.execute("SHOW server_version").fetchone()
    extensions = dict(
        super_conn.execute(
            "SELECT extname, extversion FROM pg_extension WHERE extname = ANY(%s)",
            (["vector", "btree_gist"],),
        ).fetchall()
    )
    settings = {
        str(name): f"{setting}{unit or ''}"
        for name, setting, unit in super_conn.execute(
            "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%s)", (list(GUC_NAMES),)
        ).fetchall()
    }
    hnsw: dict[str, str] = {}
    for name in ("hnsw.max_scan_tuples", "hnsw.scan_mem_multiplier"):
        row = super_conn.execute("SELECT current_setting(%s, true)", (name,)).fetchone()
        if row and row[0]:
            hnsw[name] = str(row[0])
    hnsw["hnsw.ef_search (harness)"] = str(cfg.ef_search)
    definitions = {
        str(name): str(definition)
        for name, definition in super_conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND indexname <> %s",
            (SPIKE_SCHEMA, INDEXES.fts),
        ).fetchall()
    }
    generated = super_conn.execute(
        "SELECT generation_expression FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
        (SPIKE_SCHEMA, SPIKE_TABLE, TSV_COLUMN),
    ).fetchone()
    sizes = {
        str(name): int(size)
        for name, size in super_conn.execute(
            "SELECT c.relname, pg_relation_size(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'i') ORDER BY c.relname",
            (SPIKE_SCHEMA,),
        ).fetchall()
    }
    return {
        **image_reference(cfg.compose_file),
        "server_version": str(server[0]) if server else "unknown",
        "version_string": str(version[0]) if version else "unknown",
        "extensions": extensions,
        "settings": settings,
        "hnsw_settings": hnsw,
        "index_definitions": definitions,
        "tsv_column": (
            f"{TSV_COLUMN} tsvector GENERATED ALWAYS AS ({generated[0]}) STORED"
            if generated and generated[0]
            else ""
        ),
        "relation_sizes": sizes,
    }


def _config_dict(cfg: S2Config) -> dict[str, Any]:
    return {
        "database": cfg.database,
        "principals": list(cfg.principals),
        "iterative_scan": list(cfg.iterative_scan),
        "ef_search": cfg.ef_search,
        "repeats": cfg.repeats,
        "recall_queries": cfg.recall_queries,
        "maintenance_work_mem": cfg.maintenance_work_mem,
        "result_limit": RESULT_LIMIT,
        "ts_config": TS_CONFIG,
        "hnsw_m": HNSW_M,
        "hnsw_ef_construction": HNSW_EF_CONSTRUCTION,
    }


# --- run ------------------------------------------------------------------------------


def run(
    cfg: S2Config,
    admin: AdminSettings,
    *,
    admin_conn: psycopg.Connection[Any] | None = None,
    admin_user: str | None = None,
    progress: Progress | None = None,
) -> S2Report:
    """The whole S2 procedure; returns the report (``write_report`` saves it).

    ``admin_conn`` is an autocommit superuser connection; without one the harness opens
    its own from ``admin`` (host, port, admin password, admin database, TLS settings) as
    ``admin_user`` (default ``CAIRN_DB_ADMIN_USER``). The
    roles must already exist (``cairn-db bootstrap``). A dataset is generated into a
    temporary directory when ``cfg.dataset`` is ``None``.
    """
    say: Progress = progress if progress is not None else (lambda _message: None)
    own_conn = admin_conn is None
    conn = admin_conn if admin_conn is not None else _open_admin(admin, admin_user)
    try:
        with tempfile.TemporaryDirectory(prefix="cairn-s2-") as scratch:
            return _run(cfg, admin, conn, Path(scratch), say)
    finally:
        if cfg.drop_after:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(cfg.database)
                )
            )
        if own_conn:
            conn.close()


def _run(
    cfg: S2Config,
    admin: AdminSettings,
    conn: psycopg.Connection[Any],
    scratch: Path,
    say: Progress,
) -> S2Report:
    if not _roles_exist(conn):
        raise HarnessError("the cairn roles are missing; run the database bootstrap first")

    if cfg.dataset is None:
        say(f"generating {cfg.rows} rows of {cfg.dim} dimensions")
        dataset = scratch / "dataset"
        generate(S1Config(rows=cfg.rows, dim=cfg.dim), dataset)
        source = f"generated by S1 defaults ({cfg.rows} rows, dim {cfg.dim})"
    else:
        dataset = cfg.dataset
        source = f"S1 dataset {dataset}"
    info = _dataset_info(dataset, source)
    manifest_principals: dict[str, dict[str, Any]] = info["principals"]
    unknown = [p for p in cfg.principals if p not in manifest_principals]
    if unknown:
        raise HarnessError(
            f"principal(s) {', '.join(unknown)} are not in the dataset "
            f"(it has {', '.join(manifest_principals)})"
        )
    groups = {p: [int(g) for g in manifest_principals[p]["groups"]] for p in cfg.principals}
    queries = _read_queries(dataset, max(cfg.recall_queries, 1))
    if not queries:
        raise HarnessError("the dataset has no queries")

    say(f"recreating {cfg.database}")
    recreate_database(conn, cfg.database)

    def connect(user: str, password: str | None) -> Callable[[], psycopg.Connection[Any]]:
        return lambda: _connect(conn, cfg.database, user, password)

    connect_owner = connect(OWNER_ROLE, admin.owner_db_password.get_secret_value())
    connect_app = connect(APP_ROLE, admin.app_db_password.get_secret_value())
    connect_super = connect(conn.info.user, conn.info.password or None)

    with connect_owner() as owner:
        lexical = build_spike(owner, dataset, cfg, say)
        enable_rls(owner)

    say("checking default deny")
    deny = check_default_deny(connect_app, connect_owner, connect_super, info["rows"])

    facts: list[PlanFacts] = []
    statements: dict[str, str] = {}
    matrix = _matrix(cfg)
    with connect_app() as app, connect_super() as superuser:
        for number, (role, variant, principal, iterative) in enumerate(matrix, start=1):
            if number % 10 == 1:
                say(f"plans {number}/{len(matrix)}")
            baseline = role == "superuser_baseline"
            names = principal if principal != NO_PRINCIPAL else cfg.principals[0]
            fact, text = measure(
                superuser if baseline else app,
                variant=variant,
                principal=principal,
                role=role,
                iterative=iterative,
                groups=groups[names],
                principal_groups=None if baseline else groups[principal],
                queries=queries,
                cfg=cfg,
            )
            facts.append(fact)
            statements.setdefault(variant, text)
        environment = collect_environment(superuser, cfg)

    return S2Report(
        date=datetime.now(UTC).date().isoformat(),
        config=_config_dict(cfg),
        environment=environment,
        dataset=info,
        lexical=lexical,
        default_deny=deny,
        facts=tuple(facts),
        queries=statements,
        summary=summarize(facts),
    )


__all__ = [
    "INDEXES",
    "HarnessError",
    "build_spike",
    "check_default_deny",
    "collect_environment",
    "enable_rls",
    "explain_once",
    "fts_statement",
    "image_reference",
    "measure",
    "recall_at_10",
    "recreate_database",
    "run",
    "tsv_column_statement",
]
