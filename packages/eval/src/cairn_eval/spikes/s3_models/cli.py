"""``cairn-s3``: the S3 model-selection spike."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import click

from cairn_eval.questions import read_questions
from cairn_eval.runners import RunContext
from cairn_eval.seed import DEFAULT_SCENARIO, load_kestrel
from cairn_eval.spikes.s3_models.run import (
    corpus_chunks,
    measure_embedder,
    measure_reranker,
    write_report,
)
from cairn_retrieval.models.cache import CacheMiss, CachingEmbedder, CachingReranker, ModelCache
from cairn_retrieval.models.fake import FakeEmbedder, FakeReranker


@click.group()
def main() -> None:
    """S3: embedding and rerank candidates."""


@main.command(name="run")
@click.option(
    "--corpus", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--questions", "qfile", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--mode", type=click.Choice(["fake", "replay", "live"]), default="fake")
@click.option("--out", default="docs/spikes/S3", type=click.Path(path_type=Path))
def run_cmd(corpus: Path, qfile: Path, mode: str, out: Path) -> None:
    manifest: dict[str, Any] = json.loads((corpus / "manifest.json").read_text())
    questions = read_questions(qfile)
    chunks = corpus_chunks(corpus, manifest)
    ctx = RunContext(load_kestrel(DEFAULT_SCENARIO), manifest, None, FakeEmbedder(), None)  # type: ignore[arg-type]

    async def go() -> Any:
        embeds, reranks = [], []
        if mode == "fake":
            e, ranked = await measure_embedder(ctx, chunks, questions, FakeEmbedder(384), None)
            embeds.append(e)
            reranks.append(await measure_reranker(ctx, chunks, questions, ranked, FakeReranker()))
            return embeds, reranks
        from cairn_core.config import ModelCacheSettings, SearchSettings, load_settings

        s = load_settings(SearchSettings)
        cache = ModelCache(ModelCacheSettings().cache_path)
        embed_key, rerank_key = f"workers-ai:{s.embed_model}", f"workers-ai:{s.rerank_model}"
        if mode == "replay":
            emb = CachingEmbedder(None, cache, "read_only", model_key=embed_key, dim=s.embed_dim)
            rer: CachingReranker | None = CachingReranker(
                None, cache, "read_only", model_key=rerank_key
            )
        else:
            from cairn_retrieval.plan import estimate_embed, require_budget
            from cairn_retrieval.runtime import build_providers

            providers = build_providers(s)
            est = estimate_embed(s.embed_model, [t for _, t in chunks], cache, cache_key=embed_key)
            if providers.budget is not None:
                try:
                    await require_budget(providers.budget, est)
                except Exception as exc:  # InsufficientBudget
                    click.echo(
                        f"refusing: estimate {est.neurons} neurons does not fit ({exc})", err=True
                    )
                    raise SystemExit(3) from None
            emb = CachingEmbedder(providers.embedder, cache, "read_write")
            rer = (
                CachingReranker(providers.reranker, cache, "read_write")
                if providers.reranker
                else None
            )
        e, ranked = await measure_embedder(ctx, chunks, questions, emb, s.embed_model)
        embeds.append(e)
        if rer is not None:
            reranks.append(await measure_reranker(ctx, chunks, questions, ranked, rer))
        return embeds, reranks

    try:
        embeds, reranks = asyncio.run(go())
    except CacheMiss as exc:
        click.echo(f"cache_miss: {exc}", err=True)
        raise SystemExit(2) from None
    report = write_report(out, embeds, reranks, mode, dt.date.today().isoformat())
    click.echo(json.dumps({k: report[k] for k in ("mode", "chosen_embedder", "chosen_reranker")}))


if __name__ == "__main__":
    main()
