-- Phase 1 access tables (design §11 "Data Models"; PD1-03, PD1-04, PD1-05, PD1-06).
-- principals, memberships (time-aware) and acl_versions. Every table enables and forces
-- row level security. No foreign keys (ADR-0004): the store validates references.
--
-- Needs the role cairn_ingest, which `cairn-db bootstrap` creates when
-- CAIRN_INGEST_DB_PASSWORD is set.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cairn_ingest') THEN
    RAISE EXCEPTION 'role cairn_ingest is missing (set CAIRN_INGEST_DB_PASSWORD and run cairn-db bootstrap)';
  END IF;
END $$;

CREATE TABLE cairn.principals (
  principal_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  external_id  text    NOT NULL UNIQUE
               CHECK (external_id ~ '^(user|agent|team|channel|group):[a-z0-9][a-z0-9_.-]*$'),
  kind         text    NOT NULL CHECK (kind IN ('user', 'agent', 'team', 'channel', 'group')),
  display_name text    NOT NULL,
  is_external  boolean NOT NULL DEFAULT false,
  acting_for   integer,                                   -- agents only; validated by the store
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (split_part(external_id, ':', 1) = kind),
  CHECK ((kind = 'agent') = (acting_for IS NOT NULL))
);

CREATE TABLE cairn.memberships (
  member_id    integer   NOT NULL,
  group_id     integer   NOT NULL,
  valid_period tstzrange NOT NULL CHECK (NOT isempty(valid_period)),
  source       text      NOT NULL CHECK (source IN ('directory', 'chat_export')),
  PRIMARY KEY (member_id, group_id, valid_period WITHOUT OVERLAPS)
);
CREATE INDEX memberships_group_idx ON cairn.memberships (group_id);

CREATE TABLE cairn.acl_versions (
  version    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  changed_at timestamptz NOT NULL DEFAULT now(),
  reason     text NOT NULL CHECK (reason IN ('directory', 'chat_groups', 'item_acl'))
);

ALTER TABLE cairn.principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE cairn.principals FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE cairn.memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn.acl_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cairn.acl_versions FORCE ROW LEVEL SECURITY;

-- The owner runs migrations and the seed loader; it never serves requests.
CREATE POLICY owner_all ON cairn.principals FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn.memberships FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn.acl_versions FOR ALL TO cairn_owner USING (true) WITH CHECK (true);

CREATE POLICY ingest_all ON cairn.principals FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.memberships FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.acl_versions FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);

-- The API reads principals by name (session lookup), only the caller's own memberships (the
-- snapshot query), and the ACL version. nullif() because SET LOCAL leaves '' behind (S2).
CREATE POLICY app_read ON cairn.principals FOR SELECT TO cairn_app USING (true);
CREATE POLICY app_read ON cairn.memberships FOR SELECT TO cairn_app
  USING (member_id = nullif(current_setting('cairn.principal_id', true), '')::int);
CREATE POLICY app_read ON cairn.acl_versions FOR SELECT TO cairn_app USING (true);

REVOKE ALL ON cairn.principals, cairn.memberships, cairn.acl_versions FROM PUBLIC;
GRANT USAGE ON SCHEMA cairn TO cairn_ingest;
GRANT SELECT ON cairn.principals, cairn.memberships, cairn.acl_versions TO cairn_app;
GRANT SELECT, INSERT ON cairn.principals TO cairn_ingest;
GRANT UPDATE (display_name, is_external, acting_for) ON cairn.principals TO cairn_ingest;
GRANT SELECT, INSERT, DELETE ON cairn.memberships TO cairn_ingest;
GRANT UPDATE (valid_period, source) ON cairn.memberships TO cairn_ingest;
GRANT SELECT, INSERT ON cairn.acl_versions TO cairn_ingest;

-- The ingest role (importer, outbox drainer, embedding workflow) reserves neurons.
GRANT USAGE ON SCHEMA cairn_meta TO cairn_ingest;
GRANT SELECT, INSERT, UPDATE ON cairn_meta.provider_usage TO cairn_ingest;

-- Two groups every deployment has. `group:public` is in every snapshot; `group:employees` is
-- the audience of public channels and is filled by the directory import.
INSERT INTO cairn.principals (external_id, kind, display_name)
VALUES ('group:public', 'group', 'Public'), ('group:employees', 'group', 'Employees');

COMMENT ON TABLE cairn.principals IS 'Users, agents, teams, channels and groups. ACLs are arrays of principal_id.';
COMMENT ON TABLE cairn.memberships IS 'Who belongs to which group, over a valid_period. The API sees only the caller''s rows.';
