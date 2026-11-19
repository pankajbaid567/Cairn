"""``cairn-s1``: generate and load S1 datasets (design §13).

    cairn-s1 generate [--name N | --out DIR] [--rows R] [--dim D] [--overwrite] ...
    cairn-s1 hash DATASET
    cairn-s1 load DATASET [--database cairn_spike_<x>] [--table T] [--replace] [--allow-remote]

``load`` without ``--database`` builds a throwaway ``cairn_test_<hex>`` database through
``bootstrap_database``, loads into it, counts the rows, and drops it. ``--database`` keeps
the data in a ``cairn_spike_*`` database (created or re-asserted, never the content
database). Both need the compose db (``make up-db``) and connect with the settings
``cairn_core.testing`` reads; no secret is printed. ``load`` refuses a database host that is
not a loopback address or a compose service name unless ``--allow-remote`` is given
(FT-28; ``cairn_eval.spikes.host_guard``).

Exit status: 0 done, 1 refused or failed (non-empty output directory without
``--overwrite``, bad dataset, row count mismatch), 2 the command line or configuration is
wrong, the database host is remote without ``--allow-remote``, or the database cannot be
reached.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from psycopg import sql
from psycopg.conninfo import make_conninfo

from cairn_eval.spikes.host_guard import ALLOW_REMOTE_FLAG, host_allowed, refusal
from cairn_eval.spikes.s1_acl_corpus.config import ACL_MODES, S1Config
from cairn_eval.spikes.s1_acl_corpus.generate import (
    DatasetError,
    Manifest,
    OutputDirectoryNotEmpty,
    generate,
    read_manifest,
)
from cairn_eval.spikes.s1_acl_corpus.load import DEFAULT_TABLE, load, table_identifier

if TYPE_CHECKING:
    from cairn_core.testing import DbTestConfig

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
DEFAULT_ROOT: Final = Path("var/s1")
SPIKE_DB_PREFIX: Final = "cairn_spike_"


def _selectivities(text: str) -> tuple[float, ...]:
    try:
        return tuple(float(part) for part in text.split(",") if part.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a comma-separated list of numbers: {text!r}"
        ) from None


def _parser() -> argparse.ArgumentParser:
    defaults = S1Config()
    parser = argparse.ArgumentParser(prog="cairn-s1", description="S1 synthetic ACL corpus.")
    commands = parser.add_subparsers(dest="command", required=True)

    gen = commands.add_parser("generate", help="write chunks.parquet, queries.parquet, manifest")
    where = gen.add_mutually_exclusive_group()
    where.add_argument("--name", default="default", help=f"write to {DEFAULT_ROOT}/<name>")
    where.add_argument("--out", type=Path, help="write to this directory instead")
    gen.add_argument("--rows", type=int, default=defaults.rows)
    gen.add_argument("--dim", type=int, default=defaults.dim)
    gen.add_argument("--clusters", type=int, default=defaults.clusters)
    gen.add_argument(
        "--selectivities",
        type=_selectivities,
        default=defaults.selectivities,
        help="comma-separated targets in (0, 1], default 0.01,0.05,0.2,0.5",
    )
    gen.add_argument("--acl-mode", choices=ACL_MODES, default=defaults.acl_mode)
    gen.add_argument("--noise-groups", type=int, default=defaults.noise_groups)
    gen.add_argument("--batch-rows", type=int, default=defaults.batch_rows)
    gen.add_argument("--queries", type=int, default=defaults.queries)
    gen.add_argument("--seed", type=int, default=defaults.seed)
    gen.add_argument("--overwrite", action="store_true", help="replace the dataset files")

    digest = commands.add_parser("hash", help="print a dataset's content_sha256")
    digest.add_argument("dataset", type=Path)

    ld = commands.add_parser("load", help="COPY a dataset into Postgres")
    ld.add_argument("dataset", type=Path)
    ld.add_argument(
        "--database",
        help=f"keep the data in this {SPIKE_DB_PREFIX}* database (default: a throwaway one)",
    )
    ld.add_argument("--table", default=DEFAULT_TABLE)
    ld.add_argument("--replace", action="store_true", help="drop the table first if it exists")
    ld.add_argument(
        ALLOW_REMOTE_FLAG,
        action="store_true",
        help="allow a database host that is not loopback or a compose service name",
    )
    bench = commands.add_parser(
        "bench", help="vector ACL-filter strategies at 1/5/20/50% selectivity"
    )
    bench.add_argument("--rows", type=int, default=20000)
    bench.add_argument("--dim", type=int, default=384)
    bench.add_argument("--queries", type=int, default=30)
    bench.add_argument("--selectivities", type=_selectivities, default=(0.01, 0.05, 0.2, 0.5))
    bench.add_argument("--seed", type=int, default=1)
    bench.add_argument("--out", type=Path, default=Path("docs/spikes/S1"))
    bench.add_argument(ALLOW_REMOTE_FLAG, action="store_true")
    return parser


def _summary(manifest: Manifest, out_dir: Path) -> str:
    lines = [
        f"{out_dir}: {manifest.rows} rows, dim {manifest.dim}, "
        f"{manifest.row_groups} row group(s), {manifest.config.acl_mode} ACLs",
        f"content_sha256 {manifest.content_sha256}",
    ]
    for name, info in manifest.principals.items():
        lines.append(
            f"  {name:<7} groups {info['groups']!s:<8} visible {info['visible_rows']:>8} "
            f"selectivity {info['actual_selectivity']:.4f} (target {info['target_selectivity']})"
        )
    return "\n".join(lines)


def _generate_command(args: argparse.Namespace) -> int:
    try:
        cfg = S1Config(
            rows=args.rows,
            dim=args.dim,
            clusters=args.clusters,
            selectivities=args.selectivities,
            acl_mode=args.acl_mode,
            noise_groups=args.noise_groups,
            batch_rows=args.batch_rows,
            queries=args.queries,
            seed=args.seed,
        )
    except ValueError as exc:
        print(f"cairn-s1: {exc}", file=sys.stderr)
        return EXIT_USAGE
    out_dir: Path = args.out if args.out is not None else DEFAULT_ROOT / args.name
    try:
        manifest = generate(cfg, out_dir, overwrite=args.overwrite)
    except OutputDirectoryNotEmpty as exc:
        print(f"cairn-s1: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(_summary(manifest, out_dir))
    return EXIT_OK


def _hash_command(dataset: Path) -> int:
    try:
        print(read_manifest(dataset).content_sha256)
    except DatasetError as exc:
        print(f"cairn-s1: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def _load_into(conn: Any, dataset: Path, table: str, replace: bool) -> tuple[int, int]:
    """Load, then count with a separate query; returns (rows copied, rows counted)."""
    copied = load(dataset, conn, table, replace=replace)
    query = sql.SQL("SELECT count(*) FROM {}").format(table_identifier(table))
    row = conn.execute(query).fetchone()
    return copied, int(row[0])


def spike_owner_dsn(config: DbTestConfig, database: str) -> str:
    """Connection string for ``cairn_owner`` in a spike database, with the configured TLS
    settings (free-tier FT-14). It holds the owner password: never print it."""
    from cairn_core.bootstrap import OWNER_ROLE
    from cairn_core.config import libpq_tls

    return make_conninfo(
        host=config.target.host,
        port=config.target.port,
        dbname=database,
        user=OWNER_ROLE,
        password=config.settings.owner_db_password.get_secret_value(),
        application_name="cairn-s1",
        **libpq_tls(config.settings),
    )


def _load_command(args: argparse.Namespace) -> int:
    # Imported here: only this command needs the database helpers (and pytest, which
    # cairn_core.testing imports for its fixtures).
    from cairn_core.bootstrap import bootstrap, bootstrap_database
    from cairn_core.testing import (
        DatabaseUnavailable,
        connect_admin,
        load_config,
        temporary_database,
    )

    dataset: Path = args.dataset
    database: str | None = args.database
    if database is not None and not database.startswith(SPIKE_DB_PREFIX):
        print(f"cairn-s1: --database must start with {SPIKE_DB_PREFIX}", file=sys.stderr)
        return EXIT_USAGE
    try:
        expected = read_manifest(dataset).rows
    except DatasetError as exc:
        print(f"cairn-s1: {exc}", file=sys.stderr)
        return EXIT_FAILED
    try:
        config = load_config()
    except DatabaseUnavailable as exc:
        print(f"cairn-s1: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not host_allowed(config.target.host, allow_remote=args.allow_remote):
        print(refusal("cairn-s1", config.target.host), file=sys.stderr)
        return EXIT_USAGE
    try:
        admin = connect_admin(config)
    except DatabaseUnavailable as exc:
        print(f"cairn-s1: {exc}", file=sys.stderr)
        return EXIT_USAGE

    import psycopg

    try:
        with admin:
            bootstrap(admin, config.settings)
            if database is None:
                with temporary_database(admin, config, apply_migrations=False) as db:
                    with db.connect_owner() as conn:
                        copied, counted = _load_into(conn, dataset, args.table, args.replace)
                    where = f"throwaway database {db.name} (dropped)"
            else:
                bootstrap_database(admin, database)
                with psycopg.connect(spike_owner_dsn(config, database)) as conn:
                    copied, counted = _load_into(conn, dataset, args.table, args.replace)
                where = f"database {database}"
    except (DatasetError, ValueError, psycopg.Error) as exc:
        print(f"cairn-s1: load failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"loaded {copied} rows into {args.table} of {where}; count(*) = {counted}")
    if copied != expected or counted != expected:
        print(f"cairn-s1: expected {expected} rows", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit status (argparse exits 2 itself on a usage error)."""
    args = _parser().parse_args(argv)
    if args.command == "generate":
        return _generate_command(args)
    if args.command == "hash":
        return _hash_command(args.dataset)
    if args.command == "bench":
        from cairn_eval.spikes.s1_acl_corpus.bench import run

        run(
            args.rows,
            args.dim,
            args.queries,
            args.selectivities,
            args.seed,
            args.out,
            args.allow_remote,
        )
        return EXIT_OK
    if args.command == "bench":
        from cairn_eval.spikes.s1_acl_corpus.bench import run

        run(
            args.rows,
            args.dim,
            args.queries,
            args.selectivities,
            args.seed,
            args.out,
            args.allow_remote,
        )
        return EXIT_OK
    return _load_command(args)


if __name__ == "__main__":
    sys.exit(main())
