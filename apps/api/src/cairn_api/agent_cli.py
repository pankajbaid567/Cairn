"""``cairn-agent``: create a delegated agent and print its token (plan 4.8, 4.17). Laptop only.

    cairn-agent create --for asha --name claude-code [--days 7]

The agent is a principal ``agent:<user>-<name>`` with ``acting_for`` set to the user; the token is
printed once to stdout and never stored. Run it against the local database. For a deployment, run
it from a laptop with the Neon credentials after reading ``docs/mcp.md``.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import click
import psycopg

from cairn_api.agent_tokens import MAX_TTL_S, issue
from cairn_core.config import IngestSettings, SearchSettings, load_settings
from cairn_core.db import connect
from cairn_core.hosts import require_local_or_allowed

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


@click.group()
def main() -> None:
    """Delegated agent tokens."""


@main.command(name="create")
@click.option("--for", "user", required=True, help="The person the agent acts for (a user id).")
@click.option("--name", required=True, help="A short name for the agent, e.g. claude-code.")
@click.option("--days", default=7, show_default=True, type=click.IntRange(1, MAX_TTL_S // 86400))
@click.option("--allow-remote", is_flag=True, help="Allow a database that is not on this machine.")
def create(user: str, name: str, days: int, allow_remote: bool) -> None:
    if not _NAME.match(name) or not _NAME.match(user):
        raise click.UsageError("names are lower-case letters, digits and hyphens")
    ingest = load_settings(IngestSettings)
    secret = load_settings(SearchSettings).session_secret.get_secret_value().encode()
    require_local_or_allowed("cairn-agent create", ingest.db_host, allow_remote=allow_remote)
    agent = f"{user}-{name}"
    conn: psycopg.Connection[Any] = connect(
        ingest,
        ingest.ingest_db_user,
        ingest.ingest_db_password.get_secret_value(),
        application_name="cairn-agent",
    )
    with conn:
        row = conn.execute(
            "SELECT principal_id FROM cairn.principals WHERE external_id = %s", (f"user:{user}",)
        ).fetchone()
        if row is None:
            raise click.ClickException(f"no such user: {user}")
        conn.execute(
            "INSERT INTO cairn.principals (external_id, kind, display_name, acting_for) "
            "VALUES (%s, 'agent', %s, %s) ON CONFLICT (external_id) DO NOTHING",
            (f"agent:{agent}", f"{name} for {user}", int(row[0])),
        )
        conn.commit()
    click.echo(
        f"agent:{agent} acts for user:{user}; token valid {days} days (shown once):", err=True
    )
    click.echo(issue(agent, secret, days * 86400))


if __name__ == "__main__":
    main(prog_name="cairn-agent")
    sys.exit(0)
