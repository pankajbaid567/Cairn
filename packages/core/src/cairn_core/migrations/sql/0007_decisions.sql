-- Phase 2 decision memory (ADR-0003, ADR-0009; plan 2.4, 2.5, 2.11).
--
-- decision_events is the append-only log of what was said about a decision and when. decision_versions
-- is derived from it: bitemporal rectangles (valid_period x recorded_period) with a GiST exclusion
-- constraint, so two versions of one decision can never overlap on both axes. Claims are visible
-- only through a full support set (L5): the check lives in SECURITY DEFINER functions that read the
-- stored ACL copies, because a policy cannot join the RLS-protected tables it protects.

CREATE TABLE cairn.decisions (
  decision_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key          text NOT NULL UNIQUE CHECK (key ~ '^[A-Za-z][A-Za-z0-9._-]*$'),
  title        text NOT NULL,
  question     text,
  outcome      text,
  origin       text NOT NULL CHECK (origin IN ('scenario', 'extracted', 'manual')),
  verification text NOT NULL DEFAULT 'candidate' CHECK (verification IN ('candidate', 'verified', 'disputed', 'rejected')),
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cairn.decision_events (
  event_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  decision_id    bigint NOT NULL,
  seq            integer NOT NULL,                 -- order among the decision's events (ties on valid time)
  event          text NOT NULL CHECK (event IN ('proposed', 'accepted', 'rejected', 'withdrawn', 'implemented',
                   'superseded', 'deprecated', 'reverted', 'reopened', 'discussed', 'amended', 'partially_reverted')),
  valid_at       timestamptz NOT NULL,             -- when it held in reality (effective, else the event date)
  recorded_at    timestamptz NOT NULL,             -- when Cairn could first know it
  source_item_id bigint,                           -- the item that says so (validated by the store)
  source_ref     text,                             -- scenario source id or external id, for traceability
  by_key         text,                             -- superseding decision for 'superseded'
  note           text,
  acl            integer[] NOT NULL CHECK (cardinality(acl) > 0),
  UNIQUE (decision_id, seq)
);
CREATE INDEX decision_events_decision_idx ON cairn.decision_events (decision_id, recorded_at);

CREATE TABLE cairn.decision_versions (
  version_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  decision_id     bigint NOT NULL,
  status          text NOT NULL CHECK (status IN ('proposed', 'accepted', 'rejected', 'withdrawn', 'implemented',
                    'superseded', 'deprecated', 'reverted')),
  valid_period    tstzrange NOT NULL CHECK (NOT isempty(valid_period)),
  recorded_period tstzrange NOT NULL CHECK (NOT isempty(recorded_period)),
  event_id        bigint NOT NULL,
  acl             integer[] NOT NULL CHECK (cardinality(acl) > 0),
  EXCLUDE USING gist (decision_id WITH =, valid_period WITH &&, recorded_period WITH &&)
);
CREATE INDEX decision_versions_lookup_idx ON cairn.decision_versions (decision_id);

CREATE TABLE cairn.claims (
  claim_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  decision_id bigint NOT NULL,
  key         text NOT NULL,
  text        text NOT NULL,
  tier        text NOT NULL CHECK (tier IN ('T1', 'T2', 'T3', 'T4', 'T5')),
  UNIQUE (decision_id, key)
);

CREATE TABLE cairn.claim_support (
  claim_id    bigint NOT NULL,
  support_set integer NOT NULL,
  item_id     bigint NOT NULL,
  item_acl    integer[] NOT NULL CHECK (cardinality(item_acl) > 0),   -- copy of items.acl (store keeps it equal)
  source_ref  text,
  PRIMARY KEY (claim_id, support_set, item_id)
);

CREATE TABLE cairn.decision_links (
  from_decision bigint NOT NULL,
  to_decision   bigint NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('supersedes', 'partially_reverts', 'relates', 'conflicts')),
  recorded_at   timestamptz NOT NULL,
  PRIMARY KEY (from_decision, to_decision, kind)
);

CREATE TABLE cairn.anchors (
  decision_id bigint NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('repo', 'path', 'pr')),
  value       text NOT NULL,
  PRIMARY KEY (decision_id, kind, value)
);

CREATE TABLE cairn.entities (
  entity_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind         text NOT NULL CHECK (kind IN ('person', 'team', 'service', 'system', 'vendor', 'decision')),
  canonical    text NOT NULL,
  UNIQUE (kind, canonical)
);
CREATE TABLE cairn.aliases (
  alias_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id  bigint NOT NULL,
  alias      text NOT NULL,
  method     text NOT NULL CHECK (method IN ('exact', 'normalized', 'embedding', 'manual')),
  confirmed  boolean NOT NULL DEFAULT false,
  UNIQUE (entity_id, alias)
);

CREATE TABLE cairn.lineage (
  artifact_kind  text NOT NULL,
  artifact_id    text NOT NULL,
  input_item_id  bigint NOT NULL,
  input_hash     text NOT NULL,
  PRIMARY KEY (artifact_kind, artifact_id, input_item_id)
);
CREATE INDEX lineage_input_idx ON cairn.lineage (input_item_id);

-- Summaries regenerated per principal set, cached by (principal set, inputs) and rebuilt after a stale mark.
CREATE TABLE cairn.derived_artifacts (
  artifact_kind      text NOT NULL,
  artifact_id        text NOT NULL,
  principal_set_hash text NOT NULL,
  input_hash         text NOT NULL,
  body               jsonb NOT NULL,
  stale              boolean NOT NULL DEFAULT false,
  built_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (artifact_kind, artifact_id, principal_set_hash)
);

CREATE TABLE cairn.review_items (
  review_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind        text NOT NULL CHECK (kind IN ('candidate', 'conflict', 'alias', 'stale', 'dispute')),
  decision_id bigint,
  detail      jsonb NOT NULL DEFAULT '{}',
  state       text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'verified', 'edited', 'disputed', 'rejected')),
  reason      text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);

-- LLM extraction results, cached by (revision hash, model key, prompt version) so a re-run on unchanged
-- content spends no neurons (ADR-0015).
CREATE TABLE cairn.extractions (
  revision_hash  text NOT NULL CHECK (revision_hash ~ '^[0-9a-f]{64}$'),
  model_key      text NOT NULL,
  prompt_version text NOT NULL,
  result         jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (revision_hash, model_key, prompt_version)
);

-- --- visibility (L5) ---------------------------------------------------------------------------
-- STABLE SECURITY DEFINER functions owned by cairn_owner (whose owner_all policies let them read the
-- base tables). They read the caller's groups from the same setting RLS uses, so a missing, empty or
-- stale setting sees nothing.

CREATE FUNCTION cairn.decision_visible(p_decision bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT EXISTS (
    SELECT 1 FROM cairn.decision_events e
    WHERE e.decision_id = p_decision
      AND e.acl && nullif(current_setting('cairn.principal_groups', true), '')::int[]
  )
$$;

-- A claim is visible when at least one FULL support set is visible: every item of that set.
CREATE FUNCTION cairn.claim_visible(p_claim bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT EXISTS (
    SELECT 1 FROM cairn.claim_support s
    WHERE s.claim_id = p_claim
    GROUP BY s.support_set
    HAVING bool_and(s.item_acl && nullif(current_setting('cairn.principal_groups', true), '')::int[])
  )
$$;

REVOKE EXECUTE ON FUNCTION cairn.decision_visible(bigint), cairn.claim_visible(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn.decision_visible(bigint), cairn.claim_visible(bigint) TO cairn_app, cairn_owner, cairn_ingest;

-- --- RLS ----------------------------------------------------------------------------------------
ALTER TABLE cairn.decisions ENABLE ROW LEVEL SECURITY;         ALTER TABLE cairn.decisions FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.decision_events ENABLE ROW LEVEL SECURITY;   ALTER TABLE cairn.decision_events FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.decision_versions ENABLE ROW LEVEL SECURITY; ALTER TABLE cairn.decision_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.claims ENABLE ROW LEVEL SECURITY;            ALTER TABLE cairn.claims FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.claim_support ENABLE ROW LEVEL SECURITY;     ALTER TABLE cairn.claim_support FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.decision_links ENABLE ROW LEVEL SECURITY;    ALTER TABLE cairn.decision_links FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.anchors ENABLE ROW LEVEL SECURITY;           ALTER TABLE cairn.anchors FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.entities ENABLE ROW LEVEL SECURITY;          ALTER TABLE cairn.entities FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.aliases ENABLE ROW LEVEL SECURITY;           ALTER TABLE cairn.aliases FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.lineage ENABLE ROW LEVEL SECURITY;           ALTER TABLE cairn.lineage FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.derived_artifacts ENABLE ROW LEVEL SECURITY; ALTER TABLE cairn.derived_artifacts FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.review_items ENABLE ROW LEVEL SECURITY;      ALTER TABLE cairn.review_items FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.extractions ENABLE ROW LEVEL SECURITY;       ALTER TABLE cairn.extractions FORCE ROW LEVEL SECURITY;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['decisions','decision_events','decision_versions','claims','claim_support','decision_links',
                           'anchors','entities','aliases','lineage','derived_artifacts','review_items','extractions']
  LOOP
    EXECUTE format('CREATE POLICY owner_all ON cairn.%I FOR ALL TO cairn_owner USING (true) WITH CHECK (true)', t);
    EXECUTE format('CREATE POLICY ingest_all ON cairn.%I FOR ALL TO cairn_ingest USING (true) WITH CHECK (true)', t);
  END LOOP;
END $$;

CREATE POLICY app_read ON cairn.decisions FOR SELECT TO cairn_app USING (cairn.decision_visible(decision_id));
CREATE POLICY app_read ON cairn.decision_events FOR SELECT TO cairn_app
  USING (acl && nullif(current_setting('cairn.principal_groups', true), '')::int[]);
CREATE POLICY app_read ON cairn.decision_versions FOR SELECT TO cairn_app
  USING (acl && nullif(current_setting('cairn.principal_groups', true), '')::int[]);
CREATE POLICY app_read ON cairn.claims FOR SELECT TO cairn_app USING (cairn.claim_visible(claim_id));
CREATE POLICY app_read ON cairn.decision_links FOR SELECT TO cairn_app
  USING (cairn.decision_visible(from_decision) AND cairn.decision_visible(to_decision));
CREATE POLICY app_read ON cairn.anchors FOR SELECT TO cairn_app USING (cairn.decision_visible(decision_id));
-- Cached summaries: only the rows of the caller's own principal set.
CREATE POLICY app_cache ON cairn.derived_artifacts FOR ALL TO cairn_app
  USING (principal_set_hash = nullif(current_setting('cairn.principal_set_hash', true), ''))
  WITH CHECK (principal_set_hash = nullif(current_setting('cairn.principal_set_hash', true), ''));

REVOKE ALL ON cairn.decisions, cairn.decision_events, cairn.decision_versions, cairn.claims, cairn.claim_support,
  cairn.decision_links, cairn.anchors, cairn.entities, cairn.aliases, cairn.lineage, cairn.derived_artifacts,
  cairn.review_items, cairn.extractions FROM PUBLIC;
GRANT SELECT ON cairn.decisions, cairn.decision_events, cairn.decision_versions, cairn.claims,
  cairn.decision_links, cairn.anchors TO cairn_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON cairn.derived_artifacts TO cairn_app;

GRANT SELECT, INSERT ON cairn.decisions, cairn.decision_events, cairn.decision_versions, cairn.claims,
  cairn.claim_support, cairn.decision_links, cairn.anchors, cairn.entities, cairn.aliases, cairn.lineage,
  cairn.review_items, cairn.extractions TO cairn_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON cairn.derived_artifacts TO cairn_ingest;
GRANT UPDATE (title, question, outcome, verification) ON cairn.decisions TO cairn_ingest;
GRANT UPDATE (recorded_period, acl) ON cairn.decision_versions TO cairn_ingest;
GRANT UPDATE (acl) ON cairn.decision_events TO cairn_ingest;
GRANT UPDATE (text, tier) ON cairn.claims TO cairn_ingest;
GRANT UPDATE (item_acl) ON cairn.claim_support TO cairn_ingest;
GRANT UPDATE (input_hash) ON cairn.lineage TO cairn_ingest;
GRANT UPDATE (title) ON cairn.decisions TO cairn_ingest;
GRANT UPDATE (confirmed) ON cairn.aliases TO cairn_ingest;
GRANT UPDATE (state, reason, resolved_at, detail) ON cairn.review_items TO cairn_ingest;
GRANT DELETE ON cairn.claim_support, cairn.lineage, cairn.decision_links, cairn.anchors TO cairn_ingest;

COMMENT ON TABLE cairn.decision_versions IS 'Derived bitemporal rectangles. Never edited by hand; rebuilt by the write helpers.';
