"""S2 SQL builders and DDL: full-text search variants, the generated column, the FTS index
and the image reference (free-tier retarget Requirements 3.1 to 3.3; no database needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn_eval.spikes.s2_rls_explain.harness import (
    fts_statement,
    image_reference,
    tsv_column_statement,
)
from cairn_eval.spikes.s2_rls_explain.queries import (
    VARIANTS,
    build_query,
    is_explicit,
    lexical_text,
    shape_of,
)

TERMS = ["t3_0", "t3_1"]


def test_variants_are_the_fts_hnsw_and_hybrid_shapes_with_and_without_the_predicate() -> None:
    assert VARIANTS == (
        "fts",
        "fts_explicit_acl",
        "hnsw",
        "hnsw_explicit_acl",
        "hybrid_rrf",
        "hybrid_rrf_explicit_acl",
    )
    assert [shape_of(v) for v in VARIANTS if not is_explicit(v)] == ["fts", "hnsw", "hybrid_rrf"]


def test_every_variant_builds_and_only_explicit_ones_carry_the_predicate() -> None:
    for variant in VARIANTS:
        text = build_query(
            variant, terms=TERMS, vector=[0.5, -0.25], groups=[1000, 1001]
        ).as_string()
        assert ("acl && '{1000,1001}'::int[]" in text) is is_explicit(variant)
        assert "LIMIT" in text


def test_fts_query_uses_websearch_to_tsquery_and_ts_rank_cd_on_the_generated_column() -> None:
    text = build_query("fts", terms=TERMS, vector=[1.0], groups=[1]).as_string()
    assert text == (
        'SELECT id, ts_rank_cd("body_tsv", q) AS score FROM "s2"."chunks", '
        "websearch_to_tsquery('simple', 't3_0 or t3_1') AS q "
        'WHERE "body_tsv" @@ q ORDER BY score DESC, id LIMIT 10'
    )


def test_hybrid_query_uses_the_fts_statement_on_its_lexical_side() -> None:
    text = build_query("hybrid_rrf_explicit_acl", terms=TERMS, vector=[1.0], groups=[7]).as_string()
    assert "FULL OUTER JOIN" in text
    assert text.count("websearch_to_tsquery('simple', 't3_0 or t3_1')") == 1
    assert text.count("ts_rank_cd(") == 1
    assert text.count("acl && '{7}'::int[]") == 2  # both sides
    assert "LIMIT 50" in text


def test_terms_are_joined_with_or_and_quoted_as_one_literal() -> None:
    assert lexical_text(["a", "b", "c"]) == "a or b or c"
    text = build_query(
        "fts", terms=["it's", "x'); DROP TABLE s2.chunks; --"], vector=[1.0], groups=[]
    )
    rendered = text.as_string()
    assert "'it''s or x''); DROP TABLE s2.chunks; --'" in rendered


def test_unknown_variant_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown S2 variant"):
        build_query("bm25", terms=["a"], vector=[1.0], groups=[])


def test_generated_column_and_fts_index_statements() -> None:
    assert tsv_column_statement().as_string() == (
        'ALTER TABLE "s2"."chunks" ADD COLUMN "body_tsv" tsvector '
        "GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED"
    )
    assert fts_statement().as_string() == (
        'CREATE INDEX "chunks_fts_idx" ON "s2"."chunks" USING gin ("body_tsv")'
    )


def test_image_reference_reads_the_pgvector_anchor_and_skips_comments(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "# the db image is pgvector/pgvector:0.0.1-pg18 in this comment only\n"
        "x-pg-image: &pg-image pgvector/pgvector:0.8.6-pg18@sha256:" + "a" * 64 + "\n"
        "services:\n  db:\n    image: *pg-image\n",
        "utf-8",
    )
    assert image_reference(compose) == {
        "image_tag": "pgvector/pgvector:0.8.6-pg18",
        "image_digest": "sha256:" + "a" * 64,
        "image_source": "docker-compose.yml (service db)",
    }


def test_image_reference_without_a_pgvector_image_is_unknown(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  db:\n    image: postgres:18\n", "utf-8")
    assert image_reference(compose)["image_tag"] == "unknown"
    assert image_reference(tmp_path / "missing.yml")["image_source"] == "not found"


def test_image_reference_of_the_repository_compose_file_names_the_pinned_image() -> None:
    compose = Path(__file__).resolve().parents[4] / "docker-compose.yml"
    found = image_reference(compose)
    assert found["image_tag"].startswith("pgvector/pgvector:")
    assert found["image_tag"].endswith("-pg18")
    assert found["image_digest"].startswith("sha256:")
