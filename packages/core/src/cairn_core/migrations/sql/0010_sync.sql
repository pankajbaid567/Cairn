-- Phase 3 live sync (plan 3.4, 3.5, 3.7, 3.8, 3.12, 3.28; the edge receiver ADR).
--
-- inbox: one thin row per webhook delivery, written by the edge receiver (role cairn_edge, INSERT
-- only) or the API's local receiver. It holds identifiers and a class, never payload text, so a
-- leaked row says only that "something changed in repo 42". The drainer re-fetches current state
-- from GitHub (fetch-on-notify), which also makes out-of-order delivery harmless.

CREATE TABLE cairn.inbox (
  delivery_id     text PRIMARY KEY CHECK (length(delivery_id) BETWEEN 1 AND 100),
  event           text NOT NULL CHECK (event ~ '^[a-z_]{1,40}$'),
  action          text CHECK (action ~ '^[a-z_]{1,40}$'),
  class           text NOT NULL CHECK (class IN ('acl', 'content')),
  priority        smallint NOT NULL CHECK (priority BETWEEN 0 AND 2),   -- 0 acl, 1 content, 2 backfill
  installation_id bigint,
  org_id          bigint,
  repo_id         bigint,
  repo_name       text CHECK (repo_name ~ '^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$'),
  object_number   bigint,                       -- PR or issue number, when the event names one
  received_at     timestamptz NOT NULL DEFAULT now(),
  applied_at      timestamptz,
  attempts        integer NOT NULL DEFAULT 0,
  last_error      text CHECK (length(last_error) <= 300)
);
CREATE INDEX inbox_pending_idx ON cairn.inbox (priority, received_at) WHERE applied_at IS NULL;
CREATE INDEX inbox_repo_idx ON cairn.inbox (repo_name, received_at);

-- Freshness per source (plan 3.12). source_key is "github:<org>/<repo>" or "import:<connector>".
CREATE TABLE cairn.sync_sources (
  source_key     text PRIMARY KEY CHECK (length(source_key) BETWEEN 3 AND 220),
  kind           text NOT NULL CHECK (kind IN ('github', 'import')),
  last_event_at  timestamptz,
  last_synced_at timestamptz,
  last_error     text CHECK (length(last_error) <= 300)
);

-- Every change that could alter an answer bumps this counter: permission syncs, content applies and
-- deletion purges. Cached answers are keyed by it (plan 3.9, L7), so any change invalidates them.
ALTER TABLE cairn.acl_versions DROP CONSTRAINT acl_versions_reason_check;
ALTER TABLE cairn.acl_versions ADD CONSTRAINT acl_versions_reason_check
  CHECK (reason IN ('directory', 'chat_groups', 'item_acl', 'github', 'content', 'purge'));

-- GitHub content and memberships (plan 3.5, 3.6, 3.7).
ALTER TABLE cairn.items DROP CONSTRAINT items_connector_check;
ALTER TABLE cairn.items ADD CONSTRAINT items_connector_check
  CHECK (connector IN ('chat_export', 'meeting_notes', 'docs_folder', 'github'));
ALTER TABLE cairn.items DROP CONSTRAINT items_source_kind_check;
ALTER TABLE cairn.items ADD CONSTRAINT items_source_kind_check
  CHECK (source_kind IN ('chat_thread', 'meeting_note', 'doc', 'adr_file', 'wiki', 'pull_request', 'issue'));
ALTER TABLE cairn.memberships DROP CONSTRAINT memberships_source_check;
ALTER TABLE cairn.memberships ADD CONSTRAINT memberships_source_check
  CHECK (source IN ('directory', 'chat_export', 'github'));

ALTER TABLE cairn.items ADD COLUMN source_key text;
UPDATE cairn.items SET source_key = 'import:' || connector WHERE source_key IS NULL;
ALTER TABLE cairn.items ALTER COLUMN source_key SET NOT NULL;
ALTER TABLE cairn.items ALTER COLUMN source_key SET DEFAULT 'import:unknown';
CREATE INDEX items_source_idx ON cairn.items (source_key);

ALTER TABLE cairn.inbox ENABLE ROW LEVEL SECURITY;        ALTER TABLE cairn.inbox FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.sync_sources ENABLE ROW LEVEL SECURITY; ALTER TABLE cairn.sync_sources FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_all ON cairn.inbox FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.inbox FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn.sync_sources FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.sync_sources FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);

REVOKE ALL ON cairn.inbox, cairn.sync_sources FROM PUBLIC;
GRANT SELECT, INSERT ON cairn.inbox TO cairn_ingest;
GRANT UPDATE (applied_at, attempts, last_error) ON cairn.inbox TO cairn_ingest;
GRANT SELECT, INSERT ON cairn.sync_sources TO cairn_ingest;
GRANT UPDATE (last_event_at, last_synced_at, last_error) ON cairn.sync_sources TO cairn_ingest;
GRANT UPDATE (source_key) ON cairn.items TO cairn_ingest;

-- The edge receiver: INSERT on the inbox and nothing else (plan 3.4). The role exists only when the
-- bootstrap was given CAIRN_EDGE_DB_PASSWORD, so this block is conditional.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cairn_edge') THEN
    EXECUTE 'GRANT USAGE ON SCHEMA cairn TO cairn_edge';
    EXECUTE 'GRANT INSERT ON cairn.inbox TO cairn_edge';
    EXECUTE 'CREATE POLICY edge_insert ON cairn.inbox FOR INSERT TO cairn_edge WITH CHECK (true)';
  END IF;
END $$;

-- What the API role may learn from the inbox, as aggregates only (no row is readable by it).
-- pending_acl: ACL-class rows received at or before `p_before` and not yet applied.
CREATE FUNCTION cairn.pending_acl_events(p_before timestamptz DEFAULT now()) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT count(*)::integer FROM cairn.inbox WHERE class = 'acl' AND applied_at IS NULL AND received_at <= p_before
$$;

CREATE FUNCTION cairn.sync_summary() RETURNS TABLE (
  pending_acl integer, pending_content integer, oldest_pending_s double precision, last_applied_at timestamptz
) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT count(*) FILTER (WHERE class = 'acl' AND applied_at IS NULL)::integer,
         count(*) FILTER (WHERE class = 'content' AND applied_at IS NULL)::integer,
         extract(epoch FROM now() - min(received_at) FILTER (WHERE applied_at IS NULL)),
         max(applied_at)
  FROM cairn.inbox
$$;

-- Freshness of one repo/source as the API shows it: how long the oldest unapplied event for the
-- source has waited, and when it last synced.
CREATE FUNCTION cairn.source_freshness(p_source text)
RETURNS TABLE (last_synced_at timestamptz, oldest_pending_s double precision)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT s.last_synced_at,
         (SELECT extract(epoch FROM now() - min(i.received_at)) FROM cairn.inbox i
           WHERE i.applied_at IS NULL AND 'github:' || i.repo_name = p_source)
  FROM cairn.sync_sources s WHERE s.source_key = p_source
$$;

REVOKE EXECUTE ON FUNCTION cairn.pending_acl_events(timestamptz), cairn.sync_summary(),
  cairn.source_freshness(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn.pending_acl_events(timestamptz), cairn.sync_summary(),
  cairn.source_freshness(text) TO cairn_app, cairn_owner, cairn_ingest;

-- Deletion purge (plan 3.11): the importer role may remove what it wrote, and nothing else.
GRANT DELETE ON cairn.embeddings, cairn.chunks, cairn.revisions, cairn.item_acls, cairn.items TO cairn_ingest;
GRANT DELETE ON cairn.outbox TO cairn_ingest;
GRANT DELETE ON cairn.decision_events, cairn.decision_versions TO cairn_ingest;  -- versions are derived; rebuilt by replay
