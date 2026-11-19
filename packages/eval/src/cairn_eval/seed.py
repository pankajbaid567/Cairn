"""Seed a database from the Kestrel Pay scenario (render, import, embed).

Used by the integration tests, the canary matrix, the eval runners and ``make freetier-seed``.
It renders the scenario into a temporary corpus, imports it as ``cairn_ingest`` with the same
connectors the CLI uses, and embeds the current chunks with the given embedder (the deterministic
fake by default), so seeding spends no neurons.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from cairn_connectors import chat_export, docs_folder, meeting_notes
from cairn_connectors.formats.directory import parse_directory
from cairn_connectors.store import (
    ImportOutcome,
    apply_chat_groups,
    apply_directory,
    import_document,
)
from cairn_eval.scenario.loader import load_scenario
from cairn_eval.scenario.model import Scenario
from cairn_eval.scenario.render import render_corpus
from cairn_retrieval.embed import embed_revision, missing_embedding_revisions
from cairn_retrieval.models.fake import FakeEmbedder
from cairn_retrieval.models.protocols import Embedder

DEFAULT_SCENARIO = (
    Path(__file__).resolve().parents[4] / "scenarios" / "kestrel_pay" / "scenario.yaml"
)


@dataclass(frozen=True)
class SeededCorpus:
    scenario: Scenario
    manifest: dict[str, Any]
    corpus_dir: Path
    outcomes: tuple[ImportOutcome, ...]

    def external_id(self, source_id: str) -> str | None:
        info = self.manifest["sources"].get(source_id, {})
        return info.get("external_id")


def load_kestrel(path: Path = DEFAULT_SCENARIO) -> Scenario:
    return Scenario.model_validate(load_scenario(path))


def import_corpus(conn: psycopg.Connection[Any], corpus: Path) -> list[ImportOutcome]:
    snapshot = parse_directory(
        (corpus / "directory" / "directory.json").read_text(encoding="utf-8")
    )
    apply_directory(conn, snapshot)
    chat_docs, groups = chat_export.load(corpus / "chat")
    apply_chat_groups(conn, groups)
    docs = [
        *chat_docs,
        *meeting_notes.load(corpus / "meetings"),
        *docs_folder.load(corpus / "docs"),
    ]
    return [import_document(conn, d) for d in docs]


async def _embed(ingest_dsn: str, embedder: Embedder) -> int:
    inserted = 0
    async with await psycopg.AsyncConnection.connect(
        ingest_dsn, autocommit=True, prepare_threshold=None
    ) as conn:
        for rid in await missing_embedding_revisions(conn, embedder.model_key):
            inserted += await embed_revision(conn, embedder, rid)
    return inserted


def seed_database(
    ingest_dsn: str,
    work_dir: Path,
    *,
    scenario_file: Path = DEFAULT_SCENARIO,
    as_of: dt.date | None = None,
    embedder: Embedder | None = None,
    embed: bool = True,
) -> SeededCorpus:
    """Render the scenario under ``work_dir/corpus`` and import (and embed) it."""
    scenario = load_kestrel(scenario_file)
    corpus = work_dir / "corpus"
    manifest = render_corpus(
        scenario,
        corpus,
        as_of=as_of,
        overwrite=True,
        scenario_sha256=hashlib.sha256(scenario_file.read_bytes()).hexdigest(),
    )
    with psycopg.connect(ingest_dsn, prepare_threshold=None) as conn:
        outcomes = import_corpus(conn, corpus)
        conn.execute("SELECT cairn_meta.seed_demo_members(1)")  # accounts and roles for the people
        conn.commit()
    rejected = [o for o in outcomes if o.error]
    if rejected:
        raise RuntimeError(
            f"seed rejected {len(rejected)} document(s): {[o.error for o in rejected]}"
        )
    if embed:
        asyncio.run(_embed(ingest_dsn, embedder or FakeEmbedder(384)))
    return SeededCorpus(scenario, manifest, corpus, tuple(outcomes))


def manifest_json(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True)
