"""``cairn-s2``: run the S2 RLS and EXPLAIN harness (design §14).

    cairn-s2 run [--dataset DIR | --rows N --dim D] [--database cairn_spike_<x>]
                 [--out DIR] [--repeats N] [--drop-after] [--allow-remote] ...

``run`` recreates the spike database, builds the indexes, applies FORCE RLS, measures every
variant and writes ``<date>-report.md`` and ``<date>-results.json`` to ``--out``
(default ``docs/spikes/S2``). It needs the compose db (``make up-db``) and the settings
``cairn_core.testing`` reads; no secret is printed. It refuses a database host that is not a
loopback address or a compose service name unless ``--allow-remote`` is given (FT-28;
``cairn_eval.spikes.host_guard``).

Exit status: 0 done and the default-deny checks held, 1 a default-deny check failed or the
run failed, 2 the command line or configuration is wrong, the database host is remote
without ``--allow-remote``, or the database cannot be reached.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from cairn_eval.spikes.host_guard import ALLOW_REMOTE_FLAG, host_allowed, refusal
from cairn_eval.spikes.s2_rls_explain.config import ITERATIVE_SCAN_VALUES, S2Config

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2


def _words(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _parser() -> argparse.ArgumentParser:
    defaults = S2Config()
    parser = argparse.ArgumentParser(prog="cairn-s2", description="S2 RLS and EXPLAIN harness.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="build the spike database, measure, write the report")
    run.add_argument("--dataset", type=Path, help="an S1 dataset directory (default: generate)")
    run.add_argument("--rows", type=int, default=defaults.rows, help="rows when generating")
    run.add_argument("--dim", type=int, default=defaults.dim, help="dimensions when generating")
    run.add_argument("--database", default=defaults.database, help="a cairn_spike_* database")
    run.add_argument(
        "--principals", type=_words, default=defaults.principals, help="comma-separated"
    )
    run.add_argument(
        "--iterative-scan",
        type=_words,
        default=defaults.iterative_scan,
        help=f"comma-separated, from {','.join(ITERATIVE_SCAN_VALUES)}",
    )
    run.add_argument("--ef-search", type=int, default=defaults.ef_search)
    run.add_argument("--repeats", type=int, default=defaults.repeats)
    run.add_argument("--recall-queries", type=int, default=defaults.recall_queries)
    run.add_argument("--maintenance-work-mem", default=defaults.maintenance_work_mem)
    run.add_argument("--out", type=Path, default=defaults.out_dir, help="report directory")
    run.add_argument("--compose-file", type=Path, help="read the image tag and digest from here")
    run.add_argument("--drop-after", action="store_true", help="drop the spike database at the end")
    run.add_argument(
        ALLOW_REMOTE_FLAG,
        action="store_true",
        help="allow a database host that is not loopback or a compose service name",
    )
    return parser


def _run_command(args: argparse.Namespace) -> int:
    # Imported here: only this command needs the database helpers (and pytest, which
    # cairn_core.testing imports for its fixtures).
    import psycopg

    from cairn_core.bootstrap import BootstrapError, bootstrap
    from cairn_core.testing import DatabaseUnavailable, connect_admin, load_config
    from cairn_eval.spikes.s2_rls_explain.harness import HarnessError, run
    from cairn_eval.spikes.s2_rls_explain.report import write_report

    try:
        cfg = S2Config(
            dataset=args.dataset,
            database=args.database,
            principals=args.principals,
            iterative_scan=args.iterative_scan,
            ef_search=args.ef_search,
            repeats=args.repeats,
            out_dir=args.out,
            rows=args.rows,
            dim=args.dim,
            recall_queries=args.recall_queries,
            maintenance_work_mem=args.maintenance_work_mem,
            drop_after=args.drop_after,
            compose_file=args.compose_file,
        )
    except ValueError as exc:
        print(f"cairn-s2: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        config = load_config()
    except DatabaseUnavailable as exc:
        print(f"cairn-s2: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not host_allowed(config.target.host, allow_remote=args.allow_remote):
        print(refusal("cairn-s2", config.target.host), file=sys.stderr)
        return EXIT_USAGE
    try:
        admin = connect_admin(config)
    except DatabaseUnavailable as exc:
        print(f"cairn-s2: {exc}", file=sys.stderr)
        return EXIT_USAGE

    def progress(message: str) -> None:
        print(f"s2: {message}", file=sys.stderr, flush=True)

    try:
        with admin:
            bootstrap(admin, config.settings)
            report = run(cfg, config.settings, admin_conn=admin, progress=progress)
    except (HarnessError, BootstrapError, ValueError, OSError, psycopg.Error) as exc:
        print(f"cairn-s2: run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    markdown, results = write_report(report, cfg.out_dir)
    print(report.summary)
    print(f"default deny: {'passed' if report.default_deny.passed else 'FAILED'}")
    print(f"report: {markdown}")
    print(f"results: {results}")
    return EXIT_OK if report.passed else EXIT_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit status (argparse exits 2 itself on a usage error)."""
    args = _parser().parse_args(argv)
    return _run_command(args)


if __name__ == "__main__":
    sys.exit(main())
