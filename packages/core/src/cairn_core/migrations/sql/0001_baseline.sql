-- Baseline migration (design §6, Requirements 6.8 and 6.9).
-- Phase 0 creates no content tables. It checks that bootstrap installed the
-- extensions, then creates schema cairn for the owner role and grants the app
-- role just enough to read the migration bookkeeping table.
-- Schema cairn_meta and its schema_migrations table are created by the runner
-- before the first migration.

DO $$
DECLARE
  missing text[];
BEGIN
  SELECT array_agg(e ORDER BY e) INTO missing
  FROM unnest(ARRAY['btree_gist', 'vector']) AS e
  WHERE NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = e);
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'missing extensions: % (run cairn-db bootstrap)', array_to_string(missing, ', ');
  END IF;
END $$;

CREATE SCHEMA cairn AUTHORIZATION cairn_owner;
REVOKE ALL ON SCHEMA cairn FROM PUBLIC;
GRANT USAGE ON SCHEMA cairn TO cairn_app;
GRANT USAGE ON SCHEMA cairn_meta TO cairn_app;
GRANT SELECT ON cairn_meta.schema_migrations TO cairn_app;
COMMENT ON SCHEMA cairn IS 'Cairn content. Every table must ENABLE and FORCE row level security.';
