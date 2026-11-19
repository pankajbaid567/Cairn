-- SaaS tenancy and identity (docs/product/saas-plan.md, Phase A).
--
-- One database now holds many organisations. Isolation has two layers that must both fail for a
-- leak: (1) the existing row-level security by principal and group, and (2) this migration's
-- RESTRICTIVE policy on every table in schema cairn: a row is visible and writable only when its
-- org_id equals the connection's `cairn.org_id` setting. A restrictive policy is ANDed with the
-- policies already there, so none of them is rewritten. The test is on session_user (the login),
-- not current_user: the older SECURITY DEFINER functions run as the owner, and with current_user
-- they would have escaped the filter; with session_user an API session stays inside its
-- organisation even through them. The owner role (migrations, admin
-- functions) and cairn_ingest (the internal outbox drainer and importer, never reachable from a
-- request) are exempt from the read filter and must name the organisation of every row they
-- write (the NOT NULL column has no value to fall back on unless the connection sets one). The
-- API role cairn_app has no exemption.
--
-- Existing data becomes organisation 1, the seeded demo organisation.

CREATE TABLE cairn_meta.orgs (
  org_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug       text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,38}$'),
  name       text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  plan       text NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'team', 'business')),
  state      text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'suspended')),
  is_demo    boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO cairn_meta.orgs (slug, name, is_demo) VALUES ('kestrel-pay', 'Kestrel Pay (demo)', true);

CREATE FUNCTION cairn.current_org() RETURNS integer
  LANGUAGE sql STABLE
  AS $$ SELECT nullif(current_setting('cairn.org_id', true), '')::int $$;

CREATE FUNCTION cairn.current_account() RETURNS uuid
  LANGUAGE sql STABLE
  AS $$ SELECT nullif(current_setting('cairn.account_id', true), '')::uuid $$;

-- cairn.inbox already has an `org_id`: the GitHub organisation of a webhook. Rename it so that
-- `org_id` means one thing everywhere: the Cairn tenant.
ALTER TABLE cairn.inbox RENAME COLUMN org_id TO github_org_id;

-- Every tenant table gets org_id (existing rows: organisation 1), a default taken from the
-- connection, and the restrictive isolation policy.
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'cairn' ORDER BY tablename LOOP
    EXECUTE format('ALTER TABLE cairn.%I ADD COLUMN org_id integer NOT NULL DEFAULT 1', t);
    EXECUTE format('ALTER TABLE cairn.%I ALTER COLUMN org_id SET DEFAULT cairn.current_org()', t);
    EXECUTE format('ALTER TABLE cairn.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE cairn.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY org_isolation ON cairn.%I AS RESTRICTIVE FOR ALL '
      'USING (org_id = cairn.current_org() OR session_user IN (''cairn_owner'', ''cairn_ingest'')) '
      'WITH CHECK (org_id = cairn.current_org() OR session_user IN (''cairn_owner'', ''cairn_ingest''))', t);
  END LOOP;
END $$;

-- Names and keys are unique per organisation, not globally.
ALTER TABLE cairn.principals DROP CONSTRAINT principals_external_id_key;
ALTER TABLE cairn.principals ADD CONSTRAINT principals_org_external_key UNIQUE (org_id, external_id);
ALTER TABLE cairn.items DROP CONSTRAINT items_connector_external_id_key;
ALTER TABLE cairn.items ADD CONSTRAINT items_org_connector_external_key UNIQUE (org_id, connector, external_id);
ALTER TABLE cairn.decisions DROP CONSTRAINT decisions_key_key;
ALTER TABLE cairn.decisions ADD CONSTRAINT decisions_org_key_key UNIQUE (org_id, key);
ALTER TABLE cairn.entities DROP CONSTRAINT entities_kind_canonical_key;
ALTER TABLE cairn.entities ADD CONSTRAINT entities_org_kind_canonical_key UNIQUE (org_id, kind, canonical);

-- ---------------------------------------------------------------------------------------------
-- Identity: accounts (a person, across organisations) and their membership in each organisation.
-- ---------------------------------------------------------------------------------------------

CREATE TABLE cairn_meta.accounts (
  account_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cognito_sub   text UNIQUE,
  email         text NOT NULL UNIQUE CHECK (email = lower(email) AND email ~ '^[^@\s]+@[^@\s]+$'),
  display_name  text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
  state         text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'disabled')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz
);

CREATE TABLE cairn_meta.org_members (
  org_id       integer NOT NULL REFERENCES cairn_meta.orgs,
  account_id   uuid    NOT NULL REFERENCES cairn_meta.accounts,
  principal_id integer NOT NULL,
  role         text    NOT NULL CHECK (role IN ('owner', 'admin', 'manager', 'member', 'auditor', 'guest')),
  state        text    NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'deactivated')),
  title        text,
  joined_at    timestamptz NOT NULL DEFAULT now(),
  deactivated_at timestamptz,
  PRIMARY KEY (org_id, account_id)
);
CREATE UNIQUE INDEX org_members_principal_idx ON cairn_meta.org_members (org_id, principal_id);

CREATE TABLE cairn_meta.invites (
  invite_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       integer NOT NULL REFERENCES cairn_meta.orgs,
  email        text NOT NULL CHECK (email = lower(email)),
  role         text NOT NULL CHECK (role IN ('admin', 'manager', 'member', 'auditor', 'guest')),
  team_ids     integer[] NOT NULL DEFAULT '{}',
  token_hash   text NOT NULL UNIQUE,
  invited_by   uuid NOT NULL REFERENCES cairn_meta.accounts,
  state        text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'accepted', 'revoked')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  accepted_at  timestamptz
);
CREATE UNIQUE INDEX invites_pending_idx ON cairn_meta.invites (org_id, email) WHERE state = 'pending';

ALTER TABLE cairn_meta.orgs ENABLE ROW LEVEL SECURITY;
ALTER TABLE cairn_meta.orgs FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn_meta.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE cairn_meta.accounts FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn_meta.org_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE cairn_meta.org_members FORCE ROW LEVEL SECURITY;
ALTER TABLE cairn_meta.invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE cairn_meta.invites FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_all ON cairn_meta.orgs FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn_meta.accounts FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn_meta.org_members FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_all ON cairn_meta.invites FOR ALL TO cairn_owner USING (true) WITH CHECK (true);

-- The API reads the caller's own account, the organisation they act in, and that organisation's
-- members and invites. Writes that cross the identity boundary (sign-up, accept an invite, create
-- an organisation) go through the SECURITY DEFINER functions below.
CREATE POLICY app_read ON cairn_meta.accounts FOR SELECT TO cairn_app
  USING (account_id = cairn.current_account()
         OR account_id IN (SELECT account_id FROM cairn_meta.org_members WHERE org_id = cairn.current_org()));
CREATE POLICY app_read ON cairn_meta.orgs FOR SELECT TO cairn_app
  USING (org_id = cairn.current_org()
         OR org_id IN (SELECT org_id FROM cairn_meta.org_members WHERE account_id = cairn.current_account()));
CREATE POLICY app_read ON cairn_meta.org_members FOR SELECT TO cairn_app
  USING (org_id = cairn.current_org() OR account_id = cairn.current_account());
CREATE POLICY app_write ON cairn_meta.org_members FOR UPDATE TO cairn_app
  USING (org_id = cairn.current_org()) WITH CHECK (org_id = cairn.current_org());
CREATE POLICY app_all ON cairn_meta.invites FOR ALL TO cairn_app
  USING (org_id = cairn.current_org()) WITH CHECK (org_id = cairn.current_org());
CREATE POLICY app_update ON cairn_meta.accounts FOR UPDATE TO cairn_app
  USING (account_id = cairn.current_account()) WITH CHECK (account_id = cairn.current_account());

REVOKE ALL ON cairn_meta.orgs, cairn_meta.accounts, cairn_meta.org_members, cairn_meta.invites FROM PUBLIC;
GRANT SELECT ON cairn_meta.orgs, cairn_meta.accounts, cairn_meta.org_members TO cairn_app;
GRANT UPDATE (display_name, last_login_at) ON cairn_meta.accounts TO cairn_app;
GRANT UPDATE (role, state, title, deactivated_at) ON cairn_meta.org_members TO cairn_app;
GRANT SELECT, INSERT, UPDATE (state, accepted_at) ON cairn_meta.invites TO cairn_app;

-- ---------------------------------------------------------------------------------------------
-- Functions that cross the identity boundary. They run as the owner with a pinned search_path.
-- ---------------------------------------------------------------------------------------------

-- A handle for the principal external id: lower-case letters, digits, dot, dash, underscore.
CREATE FUNCTION cairn_meta.handle_for(p_org integer, p_name text) RETURNS text
  LANGUAGE plpgsql
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE
  base text := regexp_replace(lower(split_part(p_name, '@', 1)), '[^a-z0-9._-]+', '-', 'g');
  cand text;
  n integer := 0;
BEGIN
  base := trim(both '-.' from base);
  IF base = '' OR base !~ '^[a-z0-9]' THEN base := 'user'; END IF;
  cand := base;
  WHILE EXISTS (SELECT 1 FROM cairn.principals WHERE org_id = p_org AND external_id = 'user:' || cand) LOOP
    n := n + 1;
    cand := base || n::text;
  END LOOP;
  RETURN cand;
END $$;

-- Create or update the account for a verified identity. Returns the account id.
CREATE FUNCTION cairn_meta.upsert_account(p_sub text, p_email text, p_name text) RETURNS uuid
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn_meta, pg_temp
  AS $$
DECLARE acct uuid;
BEGIN
  INSERT INTO cairn_meta.accounts (cognito_sub, email, display_name, last_login_at)
  VALUES (p_sub, lower(p_email), left(p_name, 80), now())
  ON CONFLICT (email) DO UPDATE
    SET cognito_sub = coalesce(cairn_meta.accounts.cognito_sub, EXCLUDED.cognito_sub),
        last_login_at = now()
  RETURNING account_id INTO acct;
  RETURN acct;
END $$;

-- Create an organisation owned by the account: the two standing groups, the owner's principal
-- and membership. Returns the new organisation id.
CREATE FUNCTION cairn_meta.create_org(p_account uuid, p_slug text, p_name text) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE
  org integer;
  who text;
  handle text;
  pid integer;
BEGIN
  -- session_user, not current_user: inside a SECURITY DEFINER function current_user is the owner.
  IF session_user <> 'cairn_owner' AND p_account IS DISTINCT FROM cairn.current_account() THEN
    RAISE EXCEPTION 'an organisation can only be created for the signed-in account';
  END IF;
  SELECT display_name INTO who FROM cairn_meta.accounts WHERE account_id = p_account AND state = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'no such active account'; END IF;
  INSERT INTO cairn_meta.orgs (slug, name) VALUES (p_slug, p_name) RETURNING org_id INTO org;
  PERFORM set_config('cairn.org_id', org::text, true);  -- the rows below belong to the new organisation
  INSERT INTO cairn.principals (external_id, kind, display_name, org_id)
    VALUES ('group:public', 'group', 'Public', org), ('group:employees', 'group', 'Employees', org);
  handle := cairn_meta.handle_for(org, (SELECT email FROM cairn_meta.accounts WHERE account_id = p_account));
  INSERT INTO cairn.principals (external_id, kind, display_name, org_id)
    VALUES ('user:' || handle, 'user', who, org) RETURNING principal_id INTO pid;
  INSERT INTO cairn.memberships (member_id, group_id, valid_period, source, org_id)
    SELECT pid, principal_id, tstzrange(now(), NULL), 'directory', org
    FROM cairn.principals WHERE org_id = org AND external_id = 'group:employees';
  INSERT INTO cairn.acl_versions (reason, org_id) VALUES ('directory', org);
  INSERT INTO cairn_meta.org_members (org_id, account_id, principal_id, role)
    VALUES (org, p_account, pid, 'owner');
  RETURN org;
END $$;

-- Add an account to an organisation. Internal: no caller check, not granted to any role; used by
-- accept_invite and by add_member below.
CREATE FUNCTION cairn_meta.add_member_internal(p_org integer, p_account uuid, p_role text) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE
  who text;
  mail text;
  handle text;
  pid integer;
BEGIN
  IF p_role NOT IN ('admin', 'manager', 'member', 'auditor', 'guest') THEN
    RAISE EXCEPTION 'invalid role';
  END IF;
  SELECT display_name, email INTO who, mail FROM cairn_meta.accounts WHERE account_id = p_account AND state = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'no such active account'; END IF;
  PERFORM set_config('cairn.org_id', p_org::text, true);
  IF EXISTS (SELECT 1 FROM cairn_meta.org_members WHERE org_id = p_org AND account_id = p_account) THEN
    UPDATE cairn_meta.org_members SET state = 'active', role = p_role, deactivated_at = NULL
      WHERE org_id = p_org AND account_id = p_account RETURNING principal_id INTO pid;
    RETURN pid;
  END IF;
  handle := cairn_meta.handle_for(p_org, mail);
  INSERT INTO cairn.principals (external_id, kind, display_name, is_external, org_id)
    VALUES ('user:' || handle, 'user', who, p_role = 'guest', p_org) RETURNING principal_id INTO pid;
  IF p_role <> 'guest' THEN
    INSERT INTO cairn.memberships (member_id, group_id, valid_period, source, org_id)
      SELECT pid, principal_id, tstzrange(now(), NULL), 'directory', p_org
      FROM cairn.principals WHERE org_id = p_org AND external_id = 'group:employees';
  END IF;
  INSERT INTO cairn.acl_versions (reason, org_id) VALUES ('directory', p_org);
  INSERT INTO cairn_meta.org_members (org_id, account_id, principal_id, role) VALUES (p_org, p_account, pid, p_role);
  RETURN pid;
END $$;

-- Add an account to the caller's current organisation. The API calls this only after it has
-- checked that the caller is an admin there; the function itself refuses any other organisation.
CREATE FUNCTION cairn_meta.add_member(p_org integer, p_account uuid, p_role text) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
BEGIN
  IF session_user <> 'cairn_owner' AND p_org IS DISTINCT FROM cairn.current_org() THEN
    RAISE EXCEPTION 'members can only be added to the current organisation';
  END IF;
  RETURN cairn_meta.add_member_internal(p_org, p_account, p_role);
END $$;

-- Redeem an invite by its token hash for a verified account. Returns the organisation id.
CREATE FUNCTION cairn_meta.accept_invite(p_token_hash text, p_account uuid) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE
  inv cairn_meta.invites%ROWTYPE;
  mail text;
BEGIN
  SELECT email INTO mail FROM cairn_meta.accounts WHERE account_id = p_account AND state = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'no such active account'; END IF;
  SELECT * INTO inv FROM cairn_meta.invites WHERE token_hash = p_token_hash FOR UPDATE;
  IF NOT FOUND OR inv.state <> 'pending' OR inv.expires_at < now() OR inv.email <> mail THEN
    RAISE EXCEPTION 'invite is not valid for this account';
  END IF;
  PERFORM cairn_meta.add_member_internal(inv.org_id, p_account, inv.role);
  UPDATE cairn_meta.invites SET state = 'accepted', accepted_at = now() WHERE invite_id = inv.invite_id;
  RETURN inv.org_id;
END $$;

REVOKE ALL ON FUNCTION cairn_meta.handle_for(integer, text), cairn_meta.upsert_account(text, text, text),
  cairn_meta.create_org(uuid, text, text), cairn_meta.add_member(integer, uuid, text),
  cairn_meta.add_member_internal(integer, uuid, text),
  cairn_meta.accept_invite(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn_meta.upsert_account(text, text, text), cairn_meta.create_org(uuid, text, text),
  cairn_meta.accept_invite(text, uuid) TO cairn_app;
GRANT EXECUTE ON FUNCTION cairn_meta.add_member(integer, uuid, text) TO cairn_app;
GRANT USAGE ON SCHEMA cairn_meta TO cairn_app;

COMMENT ON TABLE cairn_meta.orgs IS 'Organisations (tenants). Organisation 1 is the seeded demo organisation.';
COMMENT ON TABLE cairn_meta.accounts IS 'A person who can sign in, across organisations. Not content.';
COMMENT ON TABLE cairn_meta.org_members IS 'An account''s role and principal in one organisation.';
