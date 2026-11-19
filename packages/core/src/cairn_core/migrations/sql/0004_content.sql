-- Phase 1 content tables (design §11 "Data Models"; PD1-05, PD1-09, PD1-10, PD1-23).
-- items, item_acls, revisions, chunks, embeddings and the transactional outbox (ADR-0011).
-- acl columns are sorted, distinct copies of item_acls kept in step by the store (PD1-05).

CREATE TABLE cairn.items (
  item_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  connector           text NOT NULL CHECK (connector IN ('chat_export', 'meeting_notes', 'docs_folder')),
  external_id         text NOT NULL,
  source_kind         text NOT NULL CHECK (source_kind IN ('chat_thread', 'meeting_note', 'doc', 'adr_file', 'wiki')),
  title               text NOT NULL,
  acl                 integer[] NOT NULL CHECK (cardinality(acl) > 0),
  current_revision_id bigint,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connector, external_id)
);

CREATE TABLE cairn.item_acls (
  item_id      bigint  NOT NULL,
  principal_id integer NOT NULL,
  PRIMARY KEY (item_id, principal_id)
);

CREATE TABLE cairn.revisions (
  revision_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  item_id           bigint NOT NULL,
  content_hash      text   NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  text              text   NOT NULL,
  metadata          jsonb  NOT NULL DEFAULT '{}',
  source_updated_at timestamptz NOT NULL,
  recorded_at       timestamptz NOT NULL DEFAULT now(),
  acl               integer[] NOT NULL CHECK (cardinality(acl) > 0)
);
CREATE INDEX revisions_item_latest_idx ON cairn.revisions (item_id, revision_id DESC);

CREATE TABLE cairn.chunks (
  chunk_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  item_id        bigint  NOT NULL,
  revision_id    bigint  NOT NULL,
  ordinal        integer NOT NULL CHECK (ordinal >= 0),
  start_offset   integer NOT NULL CHECK (start_offset >= 0),
  end_offset     integer NOT NULL,
  text           text    NOT NULL,
  heading_path   text[]  NOT NULL DEFAULT '{}',
  token_estimate integer NOT NULL CHECK (token_estimate > 0),
  source_kind    text    NOT NULL,
  title          text    NOT NULL,
  is_current     boolean NOT NULL DEFAULT true,
  acl            integer[] NOT NULL CHECK (cardinality(acl) > 0),
  CHECK (end_offset > start_offset AND char_length(text) = end_offset - start_offset),
  UNIQUE (revision_id, ordinal)
);
CREATE INDEX chunks_item_idx ON cairn.chunks (item_id) WHERE is_current;

CREATE TABLE cairn.embeddings (
  chunk_id   bigint      NOT NULL,
  model_key  text        NOT NULL,
  embedding  vector(384) NOT NULL,
  acl        integer[]   NOT NULL CHECK (cardinality(acl) > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, model_key)
);

CREATE TABLE cairn.outbox (
  event_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind          text   NOT NULL CHECK (kind = 'embed_revision'),
  revision_id   bigint NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz,
  attempts      integer NOT NULL DEFAULT 0
);
CREATE INDEX outbox_pending_idx ON cairn.outbox (event_id) WHERE dispatched_at IS NULL;

ALTER TABLE cairn.items ENABLE ROW LEVEL SECURITY;      ALTER TABLE cairn.items FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.item_acls ENABLE ROW LEVEL SECURITY;  ALTER TABLE cairn.item_acls FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.revisions ENABLE ROW LEVEL SECURITY;  ALTER TABLE cairn.revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.chunks ENABLE ROW LEVEL SECURITY;     ALTER TABLE cairn.chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.embeddings ENABLE ROW LEVEL SECURITY; ALTER TABLE cairn.embeddings FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.outbox ENABLE ROW LEVEL SECURITY;     ALTER TABLE cairn.outbox FORCE ROW LEVEL SECURITY;

-- L3: the app role sees a row only when its acl overlaps the caller's groups. A missing,
-- empty or stale ('') setting yields NULL, so no row matches (default deny).
CREATE POLICY app_read ON cairn.items FOR SELECT TO cairn_app
  USING (acl && nullif(current_setting('cairn.principal_groups', true), '')::int[]);
CREATE POLICY app_read ON cairn.revisions FOR SELECT TO cairn_app
  USING (acl && nullif(current_setting('cairn.principal_groups', true), '')::int[]);
CREATE POLICY app_read ON cairn.chunks FOR SELECT TO cairn_app
  USING (acl && nullif(current_setting('cairn.principal_groups', true), '')::int[]);
CREATE POLICY app_read ON cairn.embeddings FOR SELECT TO cairn_app
  USING (acl && nullif(current_setting('cairn.principal_groups', true), '')::int[]);

CREATE POLICY ingest_all ON cairn.items FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.item_acls FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.revisions FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.chunks FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.embeddings FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.outbox FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);

CREATE POLICY owner_all ON cairn.items FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn.item_acls FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn.revisions FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn.chunks FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn.embeddings FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn.outbox FOR ALL TO cairn_owner USING (true) WITH CHECK (true);

REVOKE ALL ON cairn.items, cairn.item_acls, cairn.revisions, cairn.chunks, cairn.embeddings, cairn.outbox FROM PUBLIC;
GRANT SELECT ON cairn.items, cairn.revisions, cairn.chunks, cairn.embeddings TO cairn_app;

GRANT SELECT, INSERT ON cairn.items TO cairn_ingest;
GRANT UPDATE (title, acl, current_revision_id, updated_at) ON cairn.items TO cairn_ingest;
GRANT SELECT, INSERT, DELETE ON cairn.item_acls TO cairn_ingest;
GRANT SELECT, INSERT ON cairn.revisions TO cairn_ingest;
GRANT UPDATE (acl) ON cairn.revisions TO cairn_ingest;
GRANT SELECT, INSERT ON cairn.chunks TO cairn_ingest;
GRANT UPDATE (acl, is_current, title) ON cairn.chunks TO cairn_ingest;
GRANT SELECT, INSERT, DELETE ON cairn.embeddings TO cairn_ingest;
GRANT UPDATE (acl) ON cairn.embeddings TO cairn_ingest;
GRANT SELECT, INSERT ON cairn.outbox TO cairn_ingest;
GRANT UPDATE (dispatched_at, attempts) ON cairn.outbox TO cairn_ingest;
GRANT DELETE ON cairn.outbox TO cairn_ingest;

COMMENT ON COLUMN cairn.chunks.acl IS 'Sorted, distinct copy of item_acls for the item. Kept equal by the store (Property 9).';
