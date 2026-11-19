-- Organisation admin helpers, invite preview, a caller check on accept_invite, and demo members
-- (SaaS plan, Phase A).

CREATE FUNCTION cairn_meta.rename_org(p_name text) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF length(btrim(p_name)) < 2 THEN RAISE EXCEPTION 'name too short'; END IF;
  UPDATE cairn_meta.orgs SET name = left(btrim(p_name), 80) WHERE org_id = cairn.current_org();
END $$;

-- What an invite link is for, shown before sign-in. The caller must already hold the token.
CREATE FUNCTION cairn_meta.invite_preview(p_token_hash text)
RETURNS TABLE (org_name text, role text, email text)
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = cairn_meta, pg_temp
  AS $$
  SELECT o.name, i.role, i.email FROM cairn_meta.invites i JOIN cairn_meta.orgs o USING (org_id)
  WHERE i.token_hash = p_token_hash AND i.state = 'pending' AND i.expires_at > now() AND o.state = 'active'
$$;

-- accept_invite again, now refusing a caller who names another account.
CREATE OR REPLACE FUNCTION cairn_meta.accept_invite(p_token_hash text, p_account uuid) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE
  inv cairn_meta.invites%ROWTYPE;
  mail text;
BEGIN
  IF session_user <> 'cairn_owner' AND p_account IS DISTINCT FROM cairn.current_account() THEN
    RAISE EXCEPTION 'an invitation can only be accepted for the signed-in account';
  END IF;
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

-- Accounts for the demo organisation's people, so the demo personas have roles and the whole
-- product can be shown without sign-up. Idempotent. Only principals that already exist get one.
CREATE FUNCTION cairn_meta.seed_demo_members(p_org integer) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = cairn, cairn_meta, pg_temp
  AS $$
DECLARE
  r record;
  acct uuid;
  made integer := 0;
  who text;
BEGIN
  PERFORM set_config('cairn.org_id', p_org::text, true);
  FOR r IN SELECT principal_id, external_id, display_name, is_external FROM cairn.principals
           WHERE org_id = p_org AND kind = 'user'
             AND principal_id NOT IN (SELECT principal_id FROM cairn_meta.org_members WHERE org_id = p_org)
           ORDER BY principal_id LOOP
    who := substr(r.external_id, 6);
    INSERT INTO cairn_meta.accounts (email, display_name)
      VALUES (who || '@kestrel.example', r.display_name)
      ON CONFLICT (email) DO UPDATE SET display_name = EXCLUDED.display_name
      RETURNING account_id INTO acct;
    INSERT INTO cairn_meta.org_members (org_id, account_id, principal_id, role, title)
      VALUES (p_org, acct, r.principal_id,
              CASE WHEN r.is_external THEN 'guest'
                   WHEN who = 'dev' THEN 'owner'
                   WHEN who = 'priya' THEN 'admin'
                   WHEN who IN ('carla', 'nadia', 'jonas', 'lena') THEN 'manager'
                   WHEN who = 'iris' THEN 'auditor'
                   ELSE 'member' END,
              NULL);
    made := made + 1;
  END LOOP;
  RETURN made;
END $$;

REVOKE ALL ON FUNCTION cairn_meta.rename_org(text), cairn_meta.invite_preview(text),
  cairn_meta.seed_demo_members(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn_meta.rename_org(text), cairn_meta.invite_preview(text) TO cairn_app;
GRANT EXECUTE ON FUNCTION cairn_meta.seed_demo_members(integer) TO cairn_ingest;

-- Production already has the demo organisation's people: give them accounts now.
SELECT cairn_meta.seed_demo_members(1);
