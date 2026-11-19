-- Phase 1 search indexes (design §10; ADR-0013): native full-text search and pgvector HNSW.
-- Both full-text vectors are stored generated columns, so a column-level UPDATE of title
-- recomputes tsv_en without a privilege on the generated column.
ALTER TABLE cairn.chunks
  ADD COLUMN tsv_en tsvector
    GENERATED ALWAYS AS (to_tsvector('english'::regconfig, title || ' ' || text)) STORED,
  ADD COLUMN tsv_simple tsvector
    GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, text)) STORED;

CREATE INDEX chunks_tsv_en_idx ON cairn.chunks USING gin (tsv_en);
CREATE INDEX chunks_tsv_simple_idx ON cairn.chunks USING gin (tsv_simple);
CREATE INDEX chunks_acl_gin_idx ON cairn.chunks USING gin (acl);
CREATE INDEX embeddings_hnsw_idx ON cairn.embeddings
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX embeddings_acl_gin_idx ON cairn.embeddings USING gin (acl);
