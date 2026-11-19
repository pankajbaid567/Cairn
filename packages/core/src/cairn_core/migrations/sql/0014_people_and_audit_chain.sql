-- People administration and the tamper-evident audit-event chain (SaaS plan, Phase A).
--
-- 1. A helper that makes a new table tenant-safe in one call (org_id, default, forced RLS, the
--    restrictive org policy). Every table from here on uses it; the invariant test in
--    test_tenancy.py fails the build for a table that does not.
-- 2. Role and state changes, teams and invitations as functions that enforce the rules in the
--    database too (the API checks first; this is the second line).
-- 3. cairn.audit_events: an append-only, hash-chained log of what people did in an organisation.

CREATE FUNCTION cairn.tenant_table(p_table regclass) RETURNS void
  LANGUAGE plpgsql
  AS $$
DECLARE
  t text := (SELECT relname FROM pg_class WHERE oid = p_table);
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = p_table AND attname = 'org_id' AND NOT attisdropped) THEN
    EXECUTE format('ALTER TABLE %s ADD COLUMN org_id integer NOT NULL DEFAULT cairn.current_org()', p_table);
  END IF;
  EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', p_table);
  EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', p_table);
  EXECUTE format('CREATE POLICY owner_all ON %s FOR ALL TO cairn_owner USING (true) WITH CHECK (true)', p_table);
  EXECUTE format(
    'CREATE POLICY org_isolation ON %s AS RESTRICTIVE FOR ALL '
    'USING (org_id = cairn.current_org() OR session_user IN (''cairn_owner'', ''cairn_ingest'')) '
    'WITH CHECK (org_id = cairn.current_org() OR session_user IN (''cairn_owner'', ''cairn_ingest''))', p_table);
  EXECUTE format('CREATE INDEX %I ON %s (org_id)', t || '_org_idx', p_table);
END $$;

CREATE FUNCTION cairn.member_role() RETURNS text
  LANGUAGE sql STABLE
  AS $$ SELECT nullif(current_setting('cairn.role', true), '') $$;

CREATE FUNCTION cairn.current_principal() RETURNS integer
  LANGUAGE sql STABLE
  AS $$ SELECT nullif(current_setting('cairn.principal_id', true), '')::int $$;

-- Ranks: owner 5, admin 4, manager 3, member 2, auditor 1, guest 0.
CREATE FUNCTION cairn.role_rank(r text) RETURNS integer
  LANGUAGE sql IMMUTABLE
  AS $$ SELECT CASE r WHEN 'owner' THEN 5 WHEN 'admin' THEN 4 WHEN 'manager' THEN 3
                      WHEN 'member' THEN 2 WHEN 'auditor' THEN 1 WHEN 'guest' THEN 0 ELSE -1 END $$;

GRANT EXECUTE ON FUNCTION cairn.member_role(), cairn.current_principal(), cairn.role_rank(text) TO PUBLIC;

-- ---------------------------------------------------------------------------------------------
-- Audit events: append-only, hash-chained per organisation.
-- ---------------------------------------------------------------------------------------------

CREATE TABLE cairn.audit_events (
  event_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  org_seq         bigint NOT NULL CHECK (org_seq >= 1),
  at              timestamptz NOT NULL,
  actor_principal integer,
  actor_account   uuid,
  actor_role      text,
  action          text NOT NULL CHECK (action ~ '^[a-z_]+(\.[a-z_]+)+$'),
  target_type     text CHECK (target_type ~ '^[a-z_]+$'),
  target_id       text CHECK (length(target_id) <= 120),
  detail          jsonb NOT NULL DEFAULT '{}' CHECK (pg_column_size(detail) < 4096),
  outcome         text NOT NULL DEFAULT 'ok' CHECK (outcome IN ('ok', 'denied', 'failed')),
  prev_hash       text NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
  row_hash        text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$')
);
SELECT cairn.tenant_table('cairn.audit_events');
CREATE UNIQUE INDEX audit_events_seq_idx ON cairn.audit_events (org_id, org_seq);
CREATE INDEX audit_events_action_idx ON cairn.audit_events (org_id, action, org_seq DESC);

CREATE FUNCTION cairn.audit_events_append_only() RETURNS trigger
  LANGUAGE plpgsql
  AS $$ BEGIN RAISE EXCEPTION 'cairn.audit_events is append-only' USING ERRCODE = '42501'; END $$;
CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON cairn.audit_events
  FOR EACH ROW EXECUTE FUNCTION cairn.audit_events_append_only();
CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON cairn.audit_events
  FOR EACH STATEMENT EXECUTE FUNCTION cairn.audit_events_append_only();

-- The hash of a row: SHA-256 over the previous hash and every field, in a fixed order.
CREATE FUNCTION cairn.audit_row_hash(
  p_prev text, p_org integer, p_seq bigint, p_at timestamptz, p_principal integer, p_account uuid,
  p_role text, p_action text, p_target_type text, p_target_id text, p_detail jsonb, p_outcome text
) RETURNS text
  LANGUAGE sql IMMUTABLE
  AS $$
  SELECT encode(sha256(convert_to(concat_ws('|', p_prev, p_org, p_seq,
           to_char(p_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
           coalesce(p_principal::text, ''), coalesce(p_account::text, ''), coalesce(p_role, ''),
           p_action, coalesce(p_target_type, ''), coalesce(p_target_id, ''), p_detail::text, p_outcome),
         'UTF8')), 'hex')
$$;

-- The one way to write an event. It takes the next number and chains it under a per-organisation
-- lock, so two requests can never both extend the same hash.
CREATE FUNCTION cairn.record_event(
  p_action text, p_target_type text DEFAULT NULL, p_target_id text DEFAULT NULL,
  p_detail jsonb DEFAULT '{}', p_outcome text DEFAULT 'ok'
) RETURNS bigint
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE
  org integer := cairn.current_org();
  seq bigint;
  prev text;
  ts timestamptz := clock_timestamp();
BEGIN
  IF org IS NULL THEN RAISE EXCEPTION 'no organisation is set'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('cairn.audit_events:' || org::text, 0));
  SELECT e.org_seq, e.row_hash INTO seq, prev
    FROM cairn.audit_events e WHERE e.org_id = org ORDER BY e.org_seq DESC LIMIT 1;
  seq := coalesce(seq, 0) + 1;
  prev := coalesce(prev, repeat('0', 64));
  INSERT INTO cairn.audit_events (org_seq, at, actor_principal, actor_account, actor_role, action,
                                  target_type, target_id, detail, outcome, prev_hash, row_hash, org_id)
  VALUES (seq, ts, cairn.current_principal(), cairn.current_account(), cairn.member_role(), p_action,
          p_target_type, p_target_id, coalesce(p_detail, '{}'), p_outcome, prev,
          cairn.audit_row_hash(prev, org, seq, ts, cairn.current_principal(), cairn.current_account(),
                               cairn.member_role(), p_action, p_target_type, p_target_id,
                               coalesce(p_detail, '{}'), p_outcome), org);
  RETURN seq;
END $$;

-- Recompute the chain from the first event. Returns the first event whose stored hash does not
-- match (or whose predecessor link is broken), or ok with the head of the chain.
CREATE FUNCTION cairn.verify_chain()
RETURNS TABLE (ok boolean, checked bigint, first_bad_seq bigint, head_seq bigint, head_hash text)
  LANGUAGE plpgsql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE
  org integer := cairn.current_org();
  r record;
  expect_prev text := repeat('0', 64);
  expect_seq bigint := 1;
  n bigint := 0;
BEGIN
  IF org IS NULL OR cairn.member_role() NOT IN ('owner', 'admin', 'auditor') THEN
    RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
  END IF;
  FOR r IN SELECT * FROM cairn.audit_events e WHERE e.org_id = org ORDER BY e.org_seq LOOP
    n := n + 1;
    IF r.org_seq <> expect_seq OR r.prev_hash <> expect_prev OR r.row_hash <>
       cairn.audit_row_hash(r.prev_hash, org, r.org_seq, r.at, r.actor_principal, r.actor_account,
                            r.actor_role, r.action, r.target_type, r.target_id, r.detail, r.outcome) THEN
      RETURN QUERY SELECT false, n, r.org_seq, NULL::bigint, NULL::text;
      RETURN;
    END IF;
    expect_prev := r.row_hash;
    expect_seq := r.org_seq + 1;
  END LOOP;
  RETURN QUERY SELECT true, n, NULL::bigint, expect_seq - 1, CASE WHEN n = 0 THEN NULL ELSE expect_prev END;
END $$;

-- Readers: an administrator, owner or auditor reads the organisation's events; anyone else reads
-- only their own. Nobody but record_event writes.
CREATE POLICY app_read ON cairn.audit_events FOR SELECT TO cairn_app
  USING (cairn.member_role() IN ('owner', 'admin', 'auditor') OR actor_principal = cairn.current_principal());
REVOKE ALL ON cairn.audit_events FROM PUBLIC;
GRANT SELECT ON cairn.audit_events TO cairn_app;
REVOKE ALL ON FUNCTION cairn.record_event(text, text, text, jsonb, text), cairn.verify_chain(),
  cairn.audit_row_hash(text, integer, bigint, timestamptz, integer, uuid, text, text, text, text, jsonb, text),
  cairn.tenant_table(regclass) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn.record_event(text, text, text, jsonb, text), cairn.verify_chain() TO cairn_app;
GRANT EXECUTE ON FUNCTION cairn.audit_row_hash(text, integer, bigint, timestamptz, integer, uuid, text, text, text, text, jsonb, text) TO cairn_app;

-- ---------------------------------------------------------------------------------------------
-- People: invitations, roles, activation and teams. The caller's role comes from `cairn.role`,
-- which the API sets from the verified membership; the functions refuse anything the role may not do.
-- ---------------------------------------------------------------------------------------------

DROP POLICY app_all ON cairn_meta.invites;
REVOKE INSERT, UPDATE ON cairn_meta.invites FROM cairn_app;
CREATE POLICY app_read ON cairn_meta.invites FOR SELECT TO cairn_app
  USING (org_id = cairn.current_org() AND cairn.member_role() IN ('owner', 'admin'));

CREATE FUNCTION cairn_meta.create_invite(p_email text, p_role text, p_token_hash text, p_ttl_hours integer DEFAULT 168)
RETURNS uuid
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE
  org integer := cairn.current_org();
  who text := cairn.member_role();
  mail text := lower(btrim(p_email));
  inv uuid;
BEGIN
  IF org IS NULL OR who NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF p_role NOT IN ('admin', 'manager', 'member', 'auditor', 'guest') THEN RAISE EXCEPTION 'invalid role'; END IF;
  IF p_role = 'admin' AND who <> 'owner' THEN RAISE EXCEPTION 'only an owner can invite an administrator' USING ERRCODE = '42501'; END IF;
  IF mail !~ '^[^@\s]+@[^@\s]+$' THEN RAISE EXCEPTION 'invalid email'; END IF;
  IF EXISTS (SELECT 1 FROM cairn_meta.org_members m JOIN cairn_meta.accounts a USING (account_id)
             WHERE m.org_id = org AND a.email = mail AND m.state = 'active') THEN
    RAISE EXCEPTION 'already a member';
  END IF;
  IF (SELECT count(*) FROM cairn_meta.invites WHERE org_id = org AND state = 'pending') >= 100 THEN
    RAISE EXCEPTION 'too many pending invitations';
  END IF;
  UPDATE cairn_meta.invites SET state = 'revoked' WHERE org_id = org AND email = mail AND state = 'pending';
  INSERT INTO cairn_meta.invites (org_id, email, role, token_hash, invited_by, expires_at)
  VALUES (org, mail, p_role, p_token_hash, cairn.current_account(), now() + make_interval(hours => p_ttl_hours))
  RETURNING invite_id INTO inv;
  RETURN inv;
END $$;

CREATE FUNCTION cairn_meta.revoke_invite(p_invite uuid) RETURNS boolean
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  UPDATE cairn_meta.invites SET state = 'revoked'
    WHERE invite_id = p_invite AND org_id = cairn.current_org() AND state = 'pending';
  RETURN FOUND;
END $$;

-- Change a member's role. An owner may set any role; an administrator may set manager, member,
-- auditor and guest, and may not touch an owner or an administrator. Nobody changes their own
-- role, and the last active owner cannot be demoted.
CREATE FUNCTION cairn_meta.set_member_role(p_account uuid, p_role text) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE
  org integer := cairn.current_org();
  who text := cairn.member_role();
  cur text;
BEGIN
  IF org IS NULL OR who NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF p_role NOT IN ('owner', 'admin', 'manager', 'member', 'auditor', 'guest') THEN RAISE EXCEPTION 'invalid role'; END IF;
  IF p_account = cairn.current_account() THEN RAISE EXCEPTION 'you cannot change your own role' USING ERRCODE = '42501'; END IF;
  SELECT role INTO cur FROM cairn_meta.org_members WHERE org_id = org AND account_id = p_account AND state = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'no such active member'; END IF;
  IF who = 'admin' AND (cur IN ('owner', 'admin') OR p_role IN ('owner', 'admin')) THEN
    RAISE EXCEPTION 'an administrator cannot change owners or administrators' USING ERRCODE = '42501';
  END IF;
  IF cur = 'owner' AND p_role <> 'owner' AND
     (SELECT count(*) FROM cairn_meta.org_members WHERE org_id = org AND role = 'owner' AND state = 'active') <= 1 THEN
    RAISE EXCEPTION 'the last owner cannot be demoted';
  END IF;
  UPDATE cairn_meta.org_members SET role = p_role WHERE org_id = org AND account_id = p_account;
END $$;

-- Deactivate or reactivate a member. Deactivating ends every group membership now, so the
-- person's access to content stops with the next permission snapshot.
CREATE FUNCTION cairn_meta.set_member_state(p_account uuid, p_active boolean) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE
  org integer := cairn.current_org();
  who text := cairn.member_role();
  cur text;
  pid integer;
BEGIN
  IF org IS NULL OR who NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF p_account = cairn.current_account() THEN RAISE EXCEPTION 'you cannot deactivate yourself' USING ERRCODE = '42501'; END IF;
  SELECT role, principal_id INTO cur, pid FROM cairn_meta.org_members WHERE org_id = org AND account_id = p_account;
  IF NOT FOUND THEN RAISE EXCEPTION 'no such member'; END IF;
  IF who = 'admin' AND cur IN ('owner', 'admin') THEN
    RAISE EXCEPTION 'an administrator cannot change owners or administrators' USING ERRCODE = '42501';
  END IF;
  IF NOT p_active AND cur = 'owner' AND
     (SELECT count(*) FROM cairn_meta.org_members WHERE org_id = org AND role = 'owner' AND state = 'active') <= 1 THEN
    RAISE EXCEPTION 'the last owner cannot be deactivated';
  END IF;
  IF p_active THEN
    UPDATE cairn_meta.org_members SET state = 'active', deactivated_at = NULL WHERE org_id = org AND account_id = p_account;
    IF cur <> 'guest' THEN
      INSERT INTO cairn.memberships (member_id, group_id, valid_period, source, org_id)
        SELECT pid, g.principal_id, tstzrange(now(), NULL), 'directory', org
        FROM cairn.principals g WHERE g.org_id = org AND g.external_id = 'group:employees'
        ON CONFLICT DO NOTHING;
    END IF;
  ELSE
    UPDATE cairn_meta.org_members SET state = 'deactivated', deactivated_at = now() WHERE org_id = org AND account_id = p_account;
    UPDATE cairn.memberships SET valid_period = tstzrange(lower(valid_period), now())
      WHERE member_id = pid AND org_id = org AND upper_inf(valid_period) AND lower(valid_period) < now();
    DELETE FROM cairn.memberships
      WHERE member_id = pid AND org_id = org AND upper_inf(valid_period) AND lower(valid_period) >= now();
  END IF;
  INSERT INTO cairn.acl_versions (reason, org_id) VALUES ('directory', org);
END $$;

CREATE FUNCTION cairn_meta.set_member_title(p_account uuid, p_title text) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
BEGIN
  IF p_account <> cairn.current_account() AND cairn.member_role() NOT IN ('owner', 'admin') THEN
    RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
  END IF;
  UPDATE cairn_meta.org_members SET title = nullif(left(btrim(p_title), 80), '')
    WHERE org_id = cairn.current_org() AND account_id = p_account;
END $$;

-- Teams are principals of kind team; members are group memberships.
CREATE FUNCTION cairn_meta.create_team(p_slug text, p_name text) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE pid integer;
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF p_slug !~ '^[a-z0-9][a-z0-9_.-]{0,38}$' THEN RAISE EXCEPTION 'invalid team slug'; END IF;
  INSERT INTO cairn.principals (external_id, kind, display_name, org_id)
    VALUES ('team:' || p_slug, 'team', left(btrim(p_name), 80), cairn.current_org())
    RETURNING principal_id INTO pid;
  RETURN pid;
END $$;

CREATE FUNCTION cairn_meta.team_set_member(p_team integer, p_member integer, p_on boolean) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE org integer := cairn.current_org();
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF NOT EXISTS (SELECT 1 FROM cairn.principals WHERE principal_id = p_team AND org_id = org AND kind = 'team') THEN
    RAISE EXCEPTION 'no such team';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM cairn_meta.org_members WHERE org_id = org AND principal_id = p_member AND state = 'active') THEN
    RAISE EXCEPTION 'no such active member';
  END IF;
  IF p_on THEN
    IF NOT EXISTS (SELECT 1 FROM cairn.memberships WHERE member_id = p_member AND group_id = p_team AND upper_inf(valid_period)) THEN
      INSERT INTO cairn.memberships (member_id, group_id, valid_period, source, org_id)
        VALUES (p_member, p_team, tstzrange(now(), NULL), 'directory', org);
    END IF;
  ELSE
    UPDATE cairn.memberships SET valid_period = tstzrange(lower(valid_period), now())
      WHERE member_id = p_member AND group_id = p_team AND upper_inf(valid_period) AND lower(valid_period) < now();
    DELETE FROM cairn.memberships
      WHERE member_id = p_member AND group_id = p_team AND upper_inf(valid_period) AND lower(valid_period) >= now();
  END IF;
  INSERT INTO cairn.acl_versions (reason, org_id) VALUES ('directory', org);
END $$;

-- The people directory: names, roles and teams, for every active member of the organisation.
-- A guest sees only themselves.
CREATE FUNCTION cairn_meta.directory()
RETURNS TABLE (account_id uuid, principal_id integer, email text, display_name text, role text,
               state text, title text, joined_at timestamptz, team_ids integer[])
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
  SELECT m.account_id, m.principal_id, a.email, a.display_name, m.role, m.state, m.title, m.joined_at,
         coalesce((SELECT array_agg(ms.group_id ORDER BY ms.group_id) FROM cairn.memberships ms
                   JOIN cairn.principals t ON t.principal_id = ms.group_id AND t.kind = 'team'
                   WHERE ms.member_id = m.principal_id AND ms.org_id = m.org_id
                     AND ms.valid_period @> now()), '{}')
  FROM cairn_meta.org_members m JOIN cairn_meta.accounts a USING (account_id)
  WHERE m.org_id = cairn.current_org()
    AND (cairn.member_role() <> 'guest' OR m.account_id = cairn.current_account())
  ORDER BY m.state, a.display_name
$$;

CREATE FUNCTION cairn_meta.teams()
RETURNS TABLE (principal_id integer, slug text, name text, member_count integer)
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
  SELECT t.principal_id, substr(t.external_id, 6), t.display_name,
         (SELECT count(*)::integer FROM cairn.memberships ms WHERE ms.group_id = t.principal_id AND ms.valid_period @> now())
  FROM cairn.principals t WHERE t.org_id = cairn.current_org() AND t.kind = 'team'
  ORDER BY t.display_name
$$;

REVOKE ALL ON FUNCTION cairn_meta.create_invite(text, text, text, integer), cairn_meta.revoke_invite(uuid),
  cairn_meta.set_member_role(uuid, text), cairn_meta.set_member_state(uuid, boolean),
  cairn_meta.set_member_title(uuid, text), cairn_meta.create_team(text, text),
  cairn_meta.team_set_member(integer, integer, boolean), cairn_meta.directory(), cairn_meta.teams() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn_meta.create_invite(text, text, text, integer), cairn_meta.revoke_invite(uuid),
  cairn_meta.set_member_role(uuid, text), cairn_meta.set_member_state(uuid, boolean),
  cairn_meta.set_member_title(uuid, text), cairn_meta.create_team(text, text),
  cairn_meta.team_set_member(integer, integer, boolean), cairn_meta.directory(), cairn_meta.teams() TO cairn_app;

-- The app role changes membership only through the functions above.
REVOKE UPDATE (role, state, title, deactivated_at) ON cairn_meta.org_members FROM cairn_app;
DROP POLICY app_write ON cairn_meta.org_members;

COMMENT ON TABLE cairn.audit_events IS 'Append-only, hash-chained per organisation. Ids and counts only, never content.';
