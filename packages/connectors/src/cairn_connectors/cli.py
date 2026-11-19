"""``cairn-import``: load a rendered corpus into the database as ``cairn_ingest`` (design §8).

    cairn-import all DIR [--embed-inline]
    cairn-import directory|chat|meetings|docs DIR
    cairn-import wait-embeddings --timeout 300
    cairn-import reembed --missing|--all
    cairn-import status

Guards run before any write (PD1-29): a non-local database host needs ``--allow-remote`` (exit 2,
before connecting), and the database size must be below ``CAIRN_DB_SIZE_GUARD_MB`` (exit 2). A
document with an unknown ACL reference is rejected loudly and the batch ends with exit status 1.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import click
import psycopg

from cairn_connectors import chat_export, docs_folder, meeting_notes
from cairn_connectors.documents import FormatError, SourceDocument
from cairn_connectors.formats.directory import parse_directory
from cairn_connectors.store import (
    ImportOutcome,
    apply_chat_groups,
    apply_directory,
    import_document,
)
from cairn_core.config import IngestSettings, libpq_tls, load_settings
from cairn_core.db import connect
from cairn_core.hosts import require_local_or_allowed

EXIT_REJECTED = 1
EXIT_GUARD = 2
EXIT_BUDGET = 3


def _settings() -> IngestSettings:
    return load_settings(IngestSettings)


def _open(settings: IngestSettings, allow_remote: bool, command: str) -> psycopg.Connection[Any]:
    require_local_or_allowed(command, settings.db_host, allow_remote=allow_remote)
    return connect(
        settings,
        settings.ingest_db_user,
        settings.ingest_db_password.get_secret_value(),
        application_name="cairn-import",
    )


def database_size_mb(conn: psycopg.Connection[Any]) -> float:
    """Size of the content database plus the DBOS database when this role may read it."""
    total = 0
    for name in ("cairn", "cairn_dbos"):
        try:
            row = conn.execute("SELECT pg_database_size(%s)", (name,)).fetchone()
            conn.rollback()
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            continue
        if row is not None:
            total += int(row[0])
    return total / (1024 * 1024)


def _guard_size(conn: psycopg.Connection[Any], settings: IngestSettings) -> None:
    size = database_size_mb(conn)
    if size > settings.size_guard_mb:
        click.echo(
            f"refusing to write: databases use {size:.0f} MB, above CAIRN_DB_SIZE_GUARD_MB="
            f"{settings.size_guard_mb}",
            err=True,
        )
        raise SystemExit(EXIT_GUARD)


def _run_documents(conn: psycopg.Connection[Any], docs: list[SourceDocument]) -> dict[str, Any]:
    outcomes: list[ImportOutcome] = [import_document(conn, d) for d in docs]
    summary: dict[str, Any] = {
        "documents": len(docs),
        "inserted": sum(1 for o in outcomes if o.revision and o.revision.value == "insert"),
        "unchanged": sum(
            1 for o in outcomes if o.revision and o.revision.value == "skip_unchanged"
        ),
        "older": sum(1 for o in outcomes if o.revision and o.revision.value == "reject_older"),
        "acl_changed": sum(1 for o in outcomes if o.acl_changed),
        "chunks": sum(o.chunks for o in outcomes),
        "rejected": [{"external_id": o.external_id, "error": o.error} for o in outcomes if o.error],
    }
    return summary


@click.group()
def main() -> None:
    """Import rendered content into Cairn."""


def _common(fn: Any) -> Any:
    fn = click.option("--allow-remote", is_flag=True, help="Allow a non-local database host.")(fn)
    return fn


@main.command(name="directory")
@click.argument("corpus", type=click.Path(exists=True, file_okay=False, path_type=Path))
@_common
def directory_cmd(corpus: Path, allow_remote: bool) -> None:
    """Apply directory/directory.json (principals and dated memberships)."""
    settings = _settings()
    with _open(settings, allow_remote, "cairn-import directory") as conn:
        _guard_size(conn, settings)
        out = apply_directory(conn, _read_directory(corpus))
    click.echo(json.dumps(out.__dict__, sort_keys=True))


def _read_directory(corpus: Path) -> Any:
    path = corpus / "directory" / "directory.json"
    try:
        return parse_directory(path.read_text(encoding="utf-8"), source=str(path.name))
    except OSError:
        raise FormatError(f"{path.name}: cannot read file") from None


@main.command(name="chat")
@click.argument("corpus", type=click.Path(exists=True, file_okay=False, path_type=Path))
@_common
def chat_cmd(corpus: Path, allow_remote: bool) -> None:
    """Apply private-channel groups, then import chat threads."""
    settings = _settings()
    docs, groups = chat_export.load(corpus / "chat")
    with _open(settings, allow_remote, "cairn-import chat") as conn:
        _guard_size(conn, settings)
        apply_chat_groups(conn, groups)
        summary = _run_documents(conn, docs)
    click.echo(json.dumps(summary, sort_keys=True))
    if summary["rejected"]:
        raise SystemExit(EXIT_REJECTED)


@main.command(name="meetings")
@click.argument("corpus", type=click.Path(exists=True, file_okay=False, path_type=Path))
@_common
def meetings_cmd(corpus: Path, allow_remote: bool) -> None:
    """Import meeting notes."""
    settings = _settings()
    docs = meeting_notes.load(corpus / "meetings")
    with _open(settings, allow_remote, "cairn-import meetings") as conn:
        _guard_size(conn, settings)
        summary = _run_documents(conn, docs)
    click.echo(json.dumps(summary, sort_keys=True))
    if summary["rejected"]:
        raise SystemExit(EXIT_REJECTED)


@main.command(name="docs")
@click.argument("corpus", type=click.Path(exists=True, file_okay=False, path_type=Path))
@_common
def docs_cmd(corpus: Path, allow_remote: bool) -> None:
    """Import docs and ADR files."""
    settings = _settings()
    docs = docs_folder.load(corpus / "docs")
    with _open(settings, allow_remote, "cairn-import docs") as conn:
        _guard_size(conn, settings)
        summary = _run_documents(conn, docs)
    click.echo(json.dumps(summary, sort_keys=True))
    if summary["rejected"]:
        raise SystemExit(EXIT_REJECTED)


@main.command(name="all")
@click.argument("corpus", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--embed-inline", is_flag=True, help="Embed new chunks in this process.")
@_common
def all_cmd(corpus: Path, embed_inline: bool, allow_remote: bool) -> None:
    """directory, chat groups, chat, meetings, docs, in that order; one JSON summary."""
    settings = _settings()
    chat_docs, groups = chat_export.load(corpus / "chat")
    meetings = meeting_notes.load(corpus / "meetings")
    docs = docs_folder.load(corpus / "docs")
    result: dict[str, Any] = {}
    with _open(settings, allow_remote, "cairn-import all") as conn:
        _guard_size(conn, settings)
        result["directory"] = apply_directory(conn, _read_directory(corpus)).__dict__
        result["chat_groups"] = apply_chat_groups(conn, groups).__dict__
        result["chat"] = _run_documents(conn, chat_docs)
        result["meetings"] = _run_documents(conn, meetings)
        result["docs"] = _run_documents(conn, docs)
    if embed_inline:
        result["embedded"] = asyncio.run(_embed_all(settings))
    click.echo(json.dumps(result, sort_keys=True))
    if any(result[k]["rejected"] for k in ("chat", "meetings", "docs")):
        raise SystemExit(EXIT_REJECTED)


async def _embed_all(settings: IngestSettings) -> dict[str, int]:
    """Embed every revision that lacks vectors for the configured embedder (laptop only)."""
    from cairn_retrieval.bedrock.client import BedrockClient
    from cairn_retrieval.cloudflare.budget import (
        InMemoryUsageStore,
        NeuronBudget,
        PostgresUsageStore,
        UsageStore,
    )
    from cairn_retrieval.cloudflare.client import WorkersAiClient
    from cairn_retrieval.embed import embed_revision, missing_embedding_revisions
    from cairn_retrieval.models.cache import CachingEmbedder, ModelCache
    from cairn_retrieval.models.factory import build_embedder
    from cairn_retrieval.plan import InsufficientBudget, estimate_embed, require_budget

    client: WorkersAiClient | None = None
    bedrock: BedrockClient | None = None
    budget: NeuronBudget | None = None
    if settings.embedder in ("workers_ai", "bedrock"):
        store: UsageStore = InMemoryUsageStore()
        postgres = (
            settings.budget_store == "postgres"
            if settings.embedder == "workers_ai"
            else settings.bedrock_budget_store == "postgres"
        )
        if postgres:
            store = PostgresUsageStore(
                settings,
                user=settings.ingest_db_user,
                password=settings.ingest_db_password.get_secret_value(),
            )
        if settings.embedder == "workers_ai":
            budget = NeuronBudget(store, settings.daily_neurons)
            client = WorkersAiClient(settings, budget)
        else:
            budget = NeuronBudget(store, settings.bedrock_daily_micro_usd, provider="bedrock")
            bedrock = BedrockClient(settings, budget)
    embedder = build_embedder(settings, client, bedrock)
    cache = None
    if settings.cache_mode != "off":
        cache = ModelCache(settings.cache_path)
        embedder = CachingEmbedder(embedder, cache, settings.cache_mode)
    done = 0
    revisions = 0
    try:
        async with await psycopg.AsyncConnection.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.ingest_db_user,
            password=settings.ingest_db_password.get_secret_value(),
            prepare_threshold=None,
            autocommit=True,
            **libpq_tls(settings),
        ) as conn:
            todo = await missing_embedding_revisions(conn, embedder.model_key)
            if budget is not None:
                from cairn_retrieval.embed import chunks_missing_embedding

                texts: list[str] = []
                for rid in todo:
                    texts.extend(
                        t for _, t in await chunks_missing_embedding(conn, rid, embedder.model_key)
                    )
                estimate = estimate_embed(
                    settings.embed_model, texts, cache, cache_key=embedder.model_key
                )
                try:
                    await require_budget(budget, estimate)
                except InsufficientBudget as exc:
                    click.echo(
                        f"refusing: {exc.estimate.neurons} budget units needed (neurons, or micro-dollars for Bedrock), {exc.remaining} left today",  # noqa: E501
                        err=True,
                    )
                    raise SystemExit(EXIT_BUDGET) from None
            for rid in todo:
                done += await embed_revision(conn, embedder, rid)
                revisions += 1
    finally:
        if client is not None:
            await client.aclose()
    return {"revisions": revisions, "vectors": done}


@main.command(name="wait-embeddings")
@click.option("--timeout", default=300, show_default=True, type=int)
@_common
def wait_cmd(timeout: int, allow_remote: bool) -> None:
    """Exit 0 when every current chunk has a vector for the active model, 1 on timeout."""
    settings = _settings()
    deadline = time.monotonic() + timeout
    with _open(settings, allow_remote, "cairn-import wait-embeddings") as conn:
        while True:
            row = conn.execute(
                "SELECT count(*) FROM cairn.chunks c WHERE c.is_current AND NOT EXISTS ("
                "SELECT 1 FROM cairn.embeddings e WHERE e.chunk_id = c.chunk_id)"
            ).fetchone()
            conn.rollback()
            if row is not None and int(row[0]) == 0:
                click.echo("all current chunks have embeddings")
                return
            if time.monotonic() >= deadline:
                click.echo(f"timeout: {row[0] if row else '?'} chunks without embeddings", err=True)
                raise SystemExit(EXIT_REJECTED)
            time.sleep(2)


@main.command(name="reembed")
@click.option("--missing", "scope", flag_value="missing", default=True)
@click.option("--all", "scope", flag_value="all")
@_common
def reembed_cmd(scope: str, allow_remote: bool) -> None:
    """Queue embed_revision outbox rows (--missing: only revisions without vectors)."""
    settings = _settings()
    with _open(settings, allow_remote, "cairn-import reembed") as conn:
        if scope == "all":
            conn.execute("DELETE FROM cairn.embeddings")
        rows = conn.execute(
            "SELECT DISTINCT revision_id FROM cairn.chunks c WHERE c.is_current AND NOT EXISTS ("
            "SELECT 1 FROM cairn.embeddings e WHERE e.chunk_id = c.chunk_id)"
        ).fetchall()
        for (rid,) in rows:
            conn.execute(
                "INSERT INTO cairn.outbox (kind, revision_id) VALUES ('embed_revision', %s)", (rid,)
            )
        conn.commit()
    click.echo(json.dumps({"queued": len(rows)}))


@main.command(name="status")
@_common
def status_cmd(allow_remote: bool) -> None:
    """Counts and database sizes only. Never prints content."""
    settings = _settings()
    with _open(settings, allow_remote, "cairn-import status") as conn:
        counts: dict[str, int | float] = {}
        for table in ("items", "revisions", "chunks", "embeddings", "principals", "memberships"):
            row = conn.execute(f"SELECT count(*) FROM cairn.{table}").fetchone()  # noqa: S608
            counts[table] = int(row[0]) if row else 0
        row = conn.execute(
            "SELECT count(*) FROM cairn.chunks c WHERE c.is_current AND NOT EXISTS ("
            "SELECT 1 FROM cairn.embeddings e WHERE e.chunk_id = c.chunk_id)"
        ).fetchone()
        counts["chunks_without_embedding"] = int(row[0]) if row else 0
        row = conn.execute(
            "SELECT count(*) FROM cairn.outbox WHERE dispatched_at IS NULL"
        ).fetchone()
        counts["outbox_pending"] = int(row[0]) if row else 0
        counts["database_mb"] = round(database_size_mb(conn), 1)
    click.echo(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
