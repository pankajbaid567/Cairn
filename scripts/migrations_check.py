"""Migrations gate (Requirement 10.7): bootstrap a fresh database, apply every migration
twice with ``cairn-db migrate`` and fail unless the second run applies nothing.

Run it as ``make migrations-check`` (needs the compose db, ``make up-db``). It builds a
throwaway ``cairn_test_<hex>`` database through ``cairn_core.testing`` and points the real
CLI at it with ``CAIRN_DB_NAME`` (and the host, port and TLS settings of the admin
connection), so the
check runs the same code path as the compose ``db-init`` service. The database is dropped
at the end. Output names versions and exit codes only; no setting value is printed.

Exit codes: 0 pass, 1 the gate failed, 2 the database is unreachable or not configured.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from cairn_core.bootstrap import bootstrap
from cairn_core.config import CommonSettings, libpq_tls
from cairn_core.migrate import bundled_migrations, head
from cairn_core.testing import (
    DatabaseUnavailable,
    FreshDb,
    connect_admin,
    load_config,
    temporary_database,
)

# The console script `cairn-db` installed next to the interpreter of this environment.
CAIRN_DB = Path(sys.executable).parent / "cairn-db"
APPLIED_MARKER = "applied "
NOTHING_APPLIED = "nothing applied"


def cli_env(base: Mapping[str, str], db: FreshDb, settings: CommonSettings) -> dict[str, str]:
    """Environment for ``cairn-db migrate`` against ``db``: ``base`` plus the database name,
    the admin connection's host and port, and the TLS settings (free-tier FT-14)."""
    tls = libpq_tls(settings)
    env = {
        **base,
        "CAIRN_DB_NAME": db.name,
        "CAIRN_DB_HOST": db.host,
        "CAIRN_DB_PORT": str(db.port),
        "CAIRN_DB_SSLMODE": tls["sslmode"],
    }
    if "sslrootcert" in tls:
        env["CAIRN_DB_SSLROOTCERT"] = tls["sslrootcert"]
    return env


def _migrate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CAIRN_DB), "migrate"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    try:
        config = load_config()
        admin = connect_admin(config)
    except DatabaseUnavailable as exc:
        print(f"migrations-check: {exc}", file=sys.stderr)
        return 2
    expected_head = head(bundled_migrations())
    with admin:
        bootstrap(admin, config.settings)
        with temporary_database(admin, config, apply_migrations=False) as db:
            env = cli_env(os.environ, db, config.settings)
            first = _migrate(env)
            second = _migrate(env)
    print(
        f"migrations-check: first run exit {first.returncode}, second run exit {second.returncode}"
    )
    if first.returncode != 0 or second.returncode != 0:
        # The CLI never prints credentials, so its stderr is safe to show.
        print(first.stderr + second.stderr, file=sys.stderr, end="")
        return 1
    if f"now at version {expected_head}" not in first.stdout:
        print(
            f"migrations-check: the first run did not reach version {expected_head}",
            file=sys.stderr,
        )
        return 1
    if NOTHING_APPLIED not in second.stdout or any(
        line.startswith(APPLIED_MARKER) for line in second.stdout.splitlines()
    ):
        print("migrations-check: the second run applied something", file=sys.stderr)
        return 1
    print(f"migrations-check: ok (version {expected_head}; the second run applied nothing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
