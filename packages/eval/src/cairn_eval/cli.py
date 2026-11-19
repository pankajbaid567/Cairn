"""``cairn-eval``: questions, runs, the eval-smoke gate and the Phase 1 baseline (design §18).

    cairn-eval questions export --split dev --out var/eval/c1-dev.jsonl
    cairn-eval run --variant b1 --models fake --out var/eval/run1       (local database, as cairn_app)
    cairn-eval smoke [--models fake|cached]                              (fresh seeded test database)
    cairn-eval phase1 [--models fake|cached|live]                        (B0 and B1 on all dev questions)

Model modes: ``fake`` (deterministic, no network), ``cached`` (Workers AI model keys served from
the on-disk cache; a miss stops the run, exit 2), ``live`` (Workers AI; estimates first and exits 3
without a call when the estimate does not fit the laptop allowance, PD1-27).
"""  # noqa: E501

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import click
from psycopg_pool import AsyncConnectionPool

from cairn_core.hosts import require_local_or_allowed
from cairn_eval.questions import QuestionRecord, export_questions, read_questions, write_questions
from cairn_eval.results import summarize, summarize_answers, write_answers, write_run
from cairn_eval.runners import RunContext, run_variant
from cairn_eval.seed import DEFAULT_SCENARIO, SeededCorpus, load_kestrel, seed_database
from cairn_retrieval.llm.extractive import FakeProvider
from cairn_retrieval.llm.types import LlmProvider
from cairn_retrieval.models.cache import CacheMiss, CachingEmbedder, CachingReranker, ModelCache
from cairn_retrieval.models.fake import FakeEmbedder, FakeReranker
from cairn_retrieval.models.protocols import Embedder, Reranker

EXIT_FAILED = 1
EXIT_CACHE_MISS = 2
EXIT_BUDGET = 3
BASELINE = Path("docs/evidence/phase-1/eval/eval-smoke-baseline.json")
TOLERANCE = 0.05  # Proposed target: Recall@5 down more than 0.05 fails the gate
SMOKE_QUESTIONS = 20


def _models(mode: str) -> tuple[Embedder, Reranker | None, Any]:
    """Embedder, reranker and a callable returning today's reserved neurons (or ``None``)."""
    if mode == "fake":
        return FakeEmbedder(384), FakeReranker(), None
    from cairn_core.config import SearchSettings, load_settings

    s = load_settings(SearchSettings)
    cache = ModelCache(s_cache_path())
    embed_key, rerank_key = f"workers-ai:{s.embed_model}", f"workers-ai:{s.rerank_model}"
    if mode == "cached":
        return (
            CachingEmbedder(None, cache, "read_only", model_key=embed_key, dim=s.embed_dim),
            None
            if s.reranker == "none"
            else CachingReranker(None, cache, "read_only", model_key=rerank_key),
            None,
        )
    from cairn_retrieval.runtime import build_providers

    providers = build_providers(s)
    embedder = CachingEmbedder(providers.embedder, cache, "read_write")
    reranker = (
        CachingReranker(providers.reranker, cache, "read_write") if providers.reranker else None
    )
    budget = providers.budget
    used = (lambda: budget.used()) if budget else None
    return embedder, reranker, used


def _answer_provider(mode: str) -> Any:
    """The B3 answer model: the deterministic extractive fake, or the configured live provider."""
    if mode == "fake":
        return FakeProvider()
    if mode == "live":
        from cairn_core.config import SearchSettings, load_settings
        from cairn_retrieval.runtime import build_providers

        return build_providers(load_settings(SearchSettings)).answer
    raise click.ClickException("B3 needs --models fake or live: answers are not cached")


def s_cache_path() -> str:
    from cairn_core.config import ModelCacheSettings

    return ModelCacheSettings().cache_path


def _run_manifest(
    run_id: str,
    variant: str,
    mode: str,
    scenario_file: Path,
    corpus: SeededCorpus,
    embedder: Embedder,
    reranker: Reranker | None,
    questions: int,
) -> dict[str, Any]:
    lock = Path("uv.lock")
    return {
        "run_id": run_id,
        "variant": variant,
        "model_mode": mode,
        "questions": questions,
        "embedder": embedder.model_key,
        "reranker": reranker.model_key if reranker else "none",
        "scenario_sha256": hashlib.sha256(scenario_file.read_bytes()).hexdigest(),
        "uv_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else "",
        "corpus_sha256": corpus.manifest["corpus_sha256"],
        "seed": 0,
        "date": dt.date.today().isoformat(),
        "label": "fake" if mode == "fake" else mode,
    }


async def _run_on_pool(
    pool: AsyncConnectionPool[Any],
    corpus: SeededCorpus,
    embedder: Embedder,
    reranker: Reranker | None,
    variants: list[str],
    questions: list[QuestionRecord],
    used: Any,
    out: Path | None,
    mode: str,
    scenario_file: Path,
) -> dict[str, dict[str, Any]]:
    ctx = RunContext(
        corpus.scenario,
        corpus.manifest,
        pool,
        embedder,
        reranker,
        answer_provider=_answer_provider(mode) if "b3" in variants else None,
    )
    summaries: dict[str, dict[str, Any]] = {}
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    for variant in variants:
        ctx.answers = []
        rows, filled = await run_variant(ctx, run_id, variant, questions, neurons_used=used)
        summary = summarize(rows, fts_filled=filled)
        if ctx.answers:
            summary["answers"] = summarize_answers(ctx.answers)
        summaries[variant] = summary
        if out is not None:
            if ctx.answers:
                write_answers(out / variant / "answers.parquet", ctx.answers)
            write_run(
                out / variant,
                _run_manifest(
                    run_id, variant, mode, scenario_file, corpus, embedder, reranker, len(questions)
                ),
                rows,
                summary,
            )
    return summaries


def _on_fresh_db(
    variants: list[str],
    limit: int | None,
    mode: str,
    out: Path | None,
    scenario_file: Path,
    split: str = "dev",
    extra: Any = None,
) -> dict[str, dict[str, Any]]:
    """Seed a temporary database, run, drop it."""
    from cairn_core.bootstrap import bootstrap
    from cairn_core.testing import (
        DatabaseUnavailable,
        connect_admin,
        load_config,
        temporary_database,
    )

    try:
        config = load_config()
        admin = connect_admin(config)
    except DatabaseUnavailable as exc:
        raise click.ClickException(str(exc)) from None
    embedder, reranker, used = _models(mode)
    with admin:
        bootstrap(admin, config.settings)
        with temporary_database(admin, config) as db:
            import tempfile

            assert db.ingest_dsn is not None  # noqa: S101
            if embedder.dim != 384:  # the migrations make vector(384); a 1024-d model needs more
                from cairn_core.embed_dim import ensure_embedding_dimension

                with db.connect_owner() as owner:
                    ensure_embedding_dimension(owner, embedder.dim)
            with tempfile.TemporaryDirectory() as tmp:
                corpus = seed_database(
                    db.ingest_dsn, Path(tmp), scenario_file=scenario_file, embedder=embedder
                )
            if any(v in ("b2", "b3") for v in variants) or extra is not None:
                from cairn_eval.decision_seed import seed_decisions

                with db.connect_ingest() as seed_conn:
                    seed_decisions(seed_conn, corpus.scenario, corpus.manifest)
            manifest_questions = export_questions(corpus.scenario, corpus.manifest, split)
            if limit:
                manifest_questions = manifest_questions[:limit]
            pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
                db.app_dsn, min_size=1, max_size=4, kwargs={"prepare_threshold": None}, open=False
            )

            async def go() -> dict[str, dict[str, Any]]:
                await pool.open()
                try:
                    if extra is not None:
                        return await extra(pool, corpus, embedder, reranker)
                    return await _run_on_pool(
                        pool,
                        corpus,
                        embedder,
                        reranker,
                        variants,
                        manifest_questions,
                        used,
                        out,
                        mode,
                        scenario_file,
                    )
                finally:
                    await pool.close()

            return asyncio.run(go())


def _baseline_key(mode: str) -> str:
    e, r, _ = _models(mode)
    return f"{e.model_key}|{r.model_key if r else 'none'}"


@click.group()
def main() -> None:
    """Evaluation tools."""


@main.group()
def questions() -> None:
    """Question files."""


@questions.command(name="export")
@click.option(
    "--scenario", default=str(DEFAULT_SCENARIO), type=click.Path(exists=True, path_type=Path)
)
@click.option("--manifest", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--split", default="dev", type=click.Choice(["dev", "test"]))
@click.option("--out", required=True, type=click.Path(path_type=Path))
def questions_export(scenario: Path, manifest: Path, split: str, out: Path) -> None:
    records = export_questions(load_kestrel(scenario), json.loads(manifest.read_text()), split)
    write_questions(out, records)
    click.echo(f"wrote {len(records)} {split} questions to {out}")


@main.command(name="seed-decisions")
@click.option(
    "--scenario", default=str(DEFAULT_SCENARIO), type=click.Path(exists=True, path_type=Path)
)
@click.option("--corpus", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--allow-remote", is_flag=True, help="Allow a database that is not on this machine.")
def seed_decisions_command(scenario: Path, corpus: Path, allow_remote: bool) -> None:
    """Load the Kestrel Pay gold decisions, claims and links (run seed-local first)."""
    from cairn_connectors.cli import _open, _settings
    from cairn_eval.decision_seed import seed_decisions

    manifest = json.loads((corpus / "manifest.json").read_text())
    with _open(_settings(), allow_remote, "cairn-eval seed-decisions") as conn:
        stats = seed_decisions(conn, load_kestrel(scenario), manifest)
    click.echo(json.dumps(stats.__dict__, sort_keys=True))


@main.command(name="run")
@click.option("--variant", type=click.Choice(["b0", "b1"]), required=True)
@click.option("--models", "mode", type=click.Choice(["fake", "cached", "live"]), default="fake")
@click.option("--questions", "qfile", required=True, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--corpus", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--allow-remote", is_flag=True)
def run_cmd(
    variant: str, mode: str, qfile: Path, corpus: Path, out: Path, allow_remote: bool
) -> None:
    """Run one variant against the configured database as cairn_app (a seeded local database)."""
    from cairn_core.config import ApiSettings, load_settings
    from cairn_core.db import make_async_pool

    api = load_settings(ApiSettings)
    require_local_or_allowed("cairn-eval run", api.db_host, allow_remote=allow_remote)
    manifest = json.loads((corpus / "manifest.json").read_text())
    seeded = SeededCorpus(load_kestrel(), manifest, corpus, ())
    embedder, reranker, used = _models(mode)

    async def go() -> dict[str, dict[str, Any]]:
        pool = make_async_pool(
            api,
            api.app_db_user,
            api.app_db_password.get_secret_value(),
            application_name="cairn-eval",
        )
        await pool.open()
        try:
            return await _run_on_pool(
                pool,
                seeded,
                embedder,
                reranker,
                [variant],
                read_questions(qfile),
                used,
                out,
                mode,
                DEFAULT_SCENARIO,
            )
        finally:
            await pool.close()

    try:
        summary = asyncio.run(go())[variant]
    except CacheMiss as exc:
        click.echo(f"cache_miss: {exc}", err=True)
        raise SystemExit(EXIT_CACHE_MISS) from None
    click.echo(json.dumps(summary, sort_keys=True))
    if summary["leaks"] or summary["citation_failures"] or summary["degraded_rows"]:
        raise SystemExit(EXIT_FAILED)


@main.command(name="smoke")
@click.option("--models", "mode", type=click.Choice(["fake", "cached"]), default="fake")
@click.option("--baseline", "baseline_file", default=str(BASELINE), type=click.Path(path_type=Path))
@click.option(
    "--scenario", default=str(DEFAULT_SCENARIO), type=click.Path(exists=True, path_type=Path)
)
def smoke_cmd(mode: str, baseline_file: Path, scenario: Path) -> None:
    """B1 on the first 20 dev questions against a freshly seeded test database."""
    try:
        summary = _on_fresh_db(["b1"], SMOKE_QUESTIONS, mode, None, scenario)["b1"]
    except CacheMiss as exc:
        click.echo(
            f"cache_miss: {exc}; refresh the cache with a live run (make eval-phase1 EVAL_MODELS=live)",  # noqa: E501
            err=True,
        )
        raise SystemExit(EXIT_CACHE_MISS) from None
    key = _baseline_key(mode)
    problems = []
    if summary["leaks"]:
        problems.append(f"{summary['leaks']} leak(s)")
    if summary["citation_failures"]:
        problems.append(f"{summary['citation_failures']} citation failure(s)")
    if summary["degraded_rows"]:
        problems.append(f"{summary['degraded_rows']} degraded row(s)")
    baseline = json.loads(baseline_file.read_text()) if baseline_file.exists() else {}
    if key not in baseline:
        problems.append(f"no baseline for {key!r}: run make eval-phase1")
    elif summary["recall_at_5"] < baseline[key] - TOLERANCE:
        problems.append(
            f"Recall@5 {summary['recall_at_5']} is more than {TOLERANCE} below the baseline {baseline[key]}"  # noqa: E501
        )
    click.echo(json.dumps({"key": key, **summary}, sort_keys=True))
    if problems:
        click.echo("eval smoke FAILED: " + "; ".join(problems), err=True)
        raise SystemExit(EXIT_FAILED)
    click.echo("eval smoke ok")


@main.command(name="phase1")
@click.option("--models", "mode", type=click.Choice(["fake", "cached", "live"]), default="fake")
@click.option("--out", default="docs/evidence/phase-1/eval", type=click.Path(path_type=Path))
@click.option("--baseline", "baseline_file", default=str(BASELINE), type=click.Path(path_type=Path))
@click.option(
    "--scenario", default=str(DEFAULT_SCENARIO), type=click.Path(exists=True, path_type=Path)
)
def phase1_cmd(mode: str, out: Path, baseline_file: Path, scenario: Path) -> None:
    """B0 and B1 on all dev questions against a freshly seeded database; writes the baseline."""
    stamp = dt.date.today().isoformat() + "-" + mode
    try:
        summaries = _on_fresh_db(["b0", "b1"], None, mode, out / stamp, scenario)
    except CacheMiss as exc:
        click.echo(f"cache_miss: {exc}", err=True)
        raise SystemExit(EXIT_CACHE_MISS) from None
    smoke = _on_fresh_db(["b1"], SMOKE_QUESTIONS, mode, None, scenario)["b1"]
    baseline = json.loads(baseline_file.read_text()) if baseline_file.exists() else {}
    baseline[_baseline_key(mode)] = smoke["recall_at_5"]
    baseline_file.parent.mkdir(parents=True, exist_ok=True)
    baseline_file.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    click.echo(json.dumps({"mode": mode, **{v: s for v, s in summaries.items()}}, sort_keys=True))
    if any(s["leaks"] or s["citation_failures"] or s["degraded_rows"] for s in summaries.values()):
        raise SystemExit(EXIT_FAILED)


@main.command(name="phase5")
@click.option("--models", "mode", type=click.Choice(["fake", "live"]), default="fake")
@click.option("--out", default="docs/evidence/phase-5/runs", type=click.Path(path_type=Path))
@click.option("--note", default="", help="One line saved with the run (what it is for).")
@click.option(
    "--scenario", default=str(DEFAULT_SCENARIO), type=click.Path(exists=True, path_type=Path)
)
def phase5_cmd(mode: str, out: Path, note: str, scenario: Path) -> None:
    """B0 to B3 on the dev and test splits, plus the deterministic suites, into <out>/<label>/."""
    label = f"{dt.date.today().isoformat()}-{mode}"
    base = out / label
    leaks: dict[str, int] = {}
    for split in ("dev", "test"):
        summaries = _on_fresh_db(
            ["b0", "b1", "b2", "b3"], None, mode, base / split, scenario, split
        )
        for v, s in summaries.items():
            leaks[v] = leaks.get(v, 0) + int(s["leaks"]) + int(s.get("answers", {}).get("leaks", 0))
        click.echo(json.dumps({"split": split, **summaries}, sort_keys=True))
    det = _on_fresh_db([], None, mode, None, scenario, extra=_deterministic_run(leaks))
    (base / "deterministic.json").write_text(
        json.dumps(det, indent=2, sort_keys=True, default=str) + "\n"
    )
    (base / "run-info.json").write_text(
        json.dumps({"mode": mode, "note": note or _default_note(mode)}, indent=2) + "\n"
    )
    click.echo(f"wrote {base}")
    if sum(leaks.values()):
        raise SystemExit(EXIT_FAILED)


def _default_note(mode: str) -> str:
    if mode == "fake":
        return (
            "Deterministic fake models. This checks the pipeline, the metrics and the deterministic "  # noqa: E501
            "suites, and gives regression baselines. It says nothing about retrieval or answer "
            "quality with real models."
        )
    return "Workers AI models (live). Neurons reserved are recorded per variant."


def _deterministic_run(leaks: dict[str, int]) -> Any:
    from cairn_eval.deterministic import as_of_accuracy, must_not_cite
    from cairn_eval.deterministic import summarize as det_summary

    async def run(
        pool: Any, corpus: SeededCorpus, embedder: Embedder, reranker: Reranker | None
    ) -> Any:
        a = await as_of_accuracy(pool, corpus.scenario, corpus.manifest)
        c = await must_not_cite(pool, corpus.scenario, corpus.manifest, embedder, reranker)
        return det_summary(a, c, leaks)

    return run


@main.group(name="seed-dump")
def seed_dump_group() -> None:
    """Data-only seed dump, load and reset (plan 6.4)."""


@seed_dump_group.command(name="dump")
@click.option("--out", default="var/seed/kestrel.seed.gz", type=click.Path(path_type=Path))
def seed_dump_dump(out: Path) -> None:
    """Dump the local content database (as cairn_owner) to a gzip file."""
    from cairn_connectors.cli import _settings
    from cairn_core.bootstrap import OWNER_ROLE
    from cairn_core.config import AdminSettings, load_settings
    from cairn_core.db import connect
    from cairn_eval import seed_dump

    admin = load_settings(AdminSettings)
    require_local_or_allowed("cairn-eval seed-dump dump", admin.db_host, allow_remote=False)
    with connect(
        _settings(),
        OWNER_ROLE,
        admin.owner_db_password.get_secret_value(),
        application_name="cairn-seed-dump",
    ) as conn:
        counts = seed_dump.dump(conn, out)
    click.echo(json.dumps({"file": str(out), "rows": counts}, sort_keys=True))


@seed_dump_group.command(name="load")
@click.option(
    "--file",
    "src",
    default="var/seed/kestrel.seed.gz",
    type=click.Path(exists=True, path_type=Path),
)
@click.option("--reset", is_flag=True, help="Truncate the content tables first (a demo reset).")
@click.option("--allow-remote", is_flag=True, help="Allow a database that is not on this machine.")
def seed_dump_load(src: Path, reset: bool, allow_remote: bool) -> None:
    """Load the dump as cairn_owner. Spends no neurons: the embeddings are in the dump."""
    from cairn_connectors.cli import _settings
    from cairn_core.bootstrap import OWNER_ROLE
    from cairn_core.config import AdminSettings, load_settings
    from cairn_core.db import connect
    from cairn_eval import seed_dump

    admin = load_settings(AdminSettings)
    require_local_or_allowed("cairn-eval seed-dump load", admin.db_host, allow_remote=allow_remote)
    try:
        with connect(
            _settings(),
            OWNER_ROLE,
            admin.owner_db_password.get_secret_value(),
            application_name="cairn-seed-load",
        ) as conn:
            counts = seed_dump.load(conn, src, reset=reset)
    except (seed_dump.SeedTooLarge, ValueError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(json.dumps({"loaded": counts}, sort_keys=True))


@main.command(name="tables")
@click.option("--runs", default="docs/evidence/phase-5/runs", type=click.Path(path_type=Path))
@click.option("--out", default="docs/results.md", type=click.Path(path_type=Path))
@click.option(
    "--vector-study",
    default="docs/spikes/S1/2026-11-04-results.json",
    type=click.Path(path_type=Path),
)
def tables_cmd(runs: Path, out: Path, vector_study: Path) -> None:
    """Regenerate every results table from the Parquet files. Makes no provider call."""
    from cairn_eval import tables

    text = tables.build(runs, vector_study if vector_study.exists() else None)
    out.write_text(text, encoding="utf-8")
    click.echo(f"wrote {out} ({len(text.splitlines())} lines)")


@main.command(name="pick-models")
@click.option("--run-id", required=True)
@click.option("--models", "mode", type=click.Choice(["fake", "live"]), default="fake")
@click.option("--resume", is_flag=True)
@click.option("--out", default="docs/evidence/phase-1/model-pick", type=click.Path(path_type=Path))
@click.option(
    "--scenario", default=str(DEFAULT_SCENARIO), type=click.Path(exists=True, path_type=Path)
)
def pick_models_cmd(run_id: str, mode: str, resume: bool, out: Path, scenario: Path) -> None:
    """Answer and judge the first 20 dev questions with each candidate (live is user-run)."""
    from cairn_core.bootstrap import bootstrap
    from cairn_core.testing import (
        DatabaseUnavailable,
        connect_admin,
        load_config,
        temporary_database,
    )
    from cairn_eval.model_pick import SameFamily, budget_for, choose, run_pick
    from cairn_eval.runners import principal_ids
    from cairn_retrieval.answer import location_of  # noqa: F401
    from cairn_retrieval.llm.fake import FakeLlm
    from cairn_retrieval.packing import pack
    from cairn_retrieval.pipeline import search as do_search
    from cairn_retrieval.types import RetrievalConfig

    if mode == "live":
        click.echo(
            "live model pick is user-run: set the Workers AI token, then see docs/deploy.md; refusing from here",  # noqa: E501
            err=True,
        )
        raise SystemExit(EXIT_BUDGET)
    embedder, reranker, _ = _models("fake")
    candidates: dict[str, LlmProvider] = {"fake-extractive": FakeProvider()}
    judge = FakeLlm()
    try:
        config = load_config()
        admin = connect_admin(config)
    except DatabaseUnavailable as exc:
        raise click.ClickException(str(exc)) from None
    with admin:
        bootstrap(admin, config.settings)
        with temporary_database(admin, config) as db:
            import tempfile

            assert db.ingest_dsn is not None  # noqa: S101
            with tempfile.TemporaryDirectory() as tmp:
                corpus = seed_database(
                    db.ingest_dsn, Path(tmp), scenario_file=scenario, embedder=embedder
                )
            qs = export_questions(corpus.scenario, corpus.manifest, "dev")[:SMOKE_QUESTIONS]
            gold = {q.id: list(q.expected.decisions) for q in corpus.scenario.questions}
            pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
                db.app_dsn, min_size=1, max_size=4, kwargs={"prepare_threshold": None}, open=False
            )

            async def go() -> Any:
                await pool.open()
                try:
                    ids = await principal_ids(pool)
                    cfg = RetrievalConfig()
                    packs = {}
                    for q in qs:
                        res = await do_search(
                            pool,
                            embedder,
                            reranker,
                            principal_id=ids[q.person],
                            query=q.text,
                            cfg=cfg,
                        )
                        packs[q.qid] = pack(
                            res.ranked,
                            budget=cfg.pack_budget_tokens,
                            max_chunks=cfg.pack_max_chunks,
                            per_item=cfg.pack_per_item_first_pass,
                            min_score=None,
                        )
                    return await run_pick(
                        out / run_id,
                        qs,
                        packs,
                        gold,
                        candidates,
                        judge,
                        ["chat_export", "meeting_notes", "docs_folder"],
                        resume=resume,
                    )
                finally:
                    await pool.close()

            try:
                rows = asyncio.run(go())
            except SameFamily as exc:
                click.echo(str(exc), err=True)
                raise SystemExit(EXIT_FAILED) from None
    picks, chosen = choose(rows)
    click.echo(
        json.dumps(
            {
                "picks": [p.__dict__ for p in picks],
                "chosen": chosen.candidate if chosen else None,
                "budget": budget_for(chosen) if chosen else None,
                "label": "fake models: a wiring check, not a result",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
