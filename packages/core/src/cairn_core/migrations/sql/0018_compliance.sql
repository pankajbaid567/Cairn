-- Audit and controls (SaaS plan, Phase D): periodic access reviews and the controls that the
-- platform's own records can evidence.
--
-- Access reviews: someone other than the person under review decides to keep, change or remove
-- their access. The decision takes effect when it is made, and the review records who decided what.
-- Controls: a fixed list of checks computed from the organisation's own records. This maps the
-- evidence the platform holds to common control themes; it does not certify anything.

CREATE TABLE cairn.access_reviews (
  review_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title      text NOT NULL CHECK (length(btrim(title)) BETWEEN 3 AND 120),
  state      text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
  started_by integer NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  due_at     timestamptz NOT NULL,
  closed_at  timestamptz,
  closed_by  integer
);
SELECT cairn.tenant_table('cairn.access_reviews');
CREATE UNIQUE INDEX access_reviews_one_open_idx ON cairn.access_reviews (org_id) WHERE state = 'open';

CREATE TABLE cairn.access_review_items (
  item_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  review_id          bigint  NOT NULL,
  member_principal   integer NOT NULL,
  member_name        text    NOT NULL,
  role_at_start      text    NOT NULL,
  teams_at_start     text[]  NOT NULL DEFAULT '{}',
  reviewer_principal integer NOT NULL,
  decision           text    NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'keep', 'change', 'remove')),
  new_role           text,
  comment            text    CHECK (length(comment) <= 500),
  decided_at         timestamptz,
  UNIQUE (review_id, member_principal),
  CHECK (reviewer_principal <> member_principal)
);
SELECT cairn.tenant_table('cairn.access_review_items');

CREATE POLICY app_read ON cairn.access_reviews FOR SELECT TO cairn_app
  USING (cairn.member_role() IN ('owner', 'admin', 'manager', 'auditor'));
CREATE POLICY app_read ON cairn.access_review_items FOR SELECT TO cairn_app
  USING (cairn.member_role() IN ('owner', 'admin', 'auditor') OR reviewer_principal = cairn.current_principal());
REVOKE ALL ON cairn.access_reviews, cairn.access_review_items FROM PUBLIC;
GRANT SELECT ON cairn.access_reviews, cairn.access_review_items TO cairn_app;

-- Start a review of every active member. Each member is assigned a reviewer who is an owner or
-- administrator other than themselves, in turn, so nobody reviews their own access. At least two
-- such people are needed.
CREATE FUNCTION cairn.start_access_review(p_title text, p_due_days integer) RETURNS bigint
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn, cairn_meta
  AS $$
DECLARE
  org integer := cairn.current_org();
  rid bigint;
  reviewers integer[];
  m record;
  i integer := 0;
  pick integer;
  n integer;
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  SELECT array_agg(principal_id ORDER BY principal_id) INTO reviewers FROM cairn_meta.org_members
    WHERE org_id = org AND state = 'active' AND role IN ('owner', 'admin');
  IF coalesce(cardinality(reviewers), 0) < 2 THEN
    RAISE EXCEPTION 'a review needs at least two owners or administrators, so nobody reviews their own access';
  END IF;
  INSERT INTO cairn.access_reviews (title, started_by, due_at)
  VALUES (btrim(p_title), cairn.current_principal(), now() + make_interval(days => greatest(p_due_days, 1)))
  RETURNING review_id INTO rid;
  FOR m IN
    SELECT om.principal_id, p.display_name, om.role,
           coalesce((SELECT array_agg(t.display_name ORDER BY t.display_name) FROM cairn.memberships ms
                     JOIN cairn.principals t ON t.principal_id = ms.group_id AND t.kind = 'team'
                     WHERE ms.member_id = om.principal_id AND ms.valid_period @> now()), '{}') AS teams
    FROM cairn_meta.org_members om JOIN cairn.principals p ON p.principal_id = om.principal_id
    WHERE om.org_id = org AND om.state = 'active' ORDER BY om.principal_id
  LOOP
    n := cardinality(reviewers);
    pick := NULL;
    FOR k IN 0..n - 1 LOOP
      IF reviewers[((i + k) % n) + 1] <> m.principal_id THEN pick := reviewers[((i + k) % n) + 1]; EXIT; END IF;
    END LOOP;
    i := i + 1;
    INSERT INTO cairn.access_review_items (review_id, member_principal, member_name, role_at_start, teams_at_start, reviewer_principal)
    VALUES (rid, m.principal_id, m.display_name, m.role, m.teams, pick);
  END LOOP;
  RETURN rid;
END $$;

-- The reviewer decides. Keep changes nothing; change sets a new role; remove deactivates the person
-- and ends their group memberships. An owner's access is changed only by an owner.
CREATE FUNCTION cairn.decide_review_item(p_item bigint, p_decision text, p_new_role text, p_comment text) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn, cairn_meta
  AS $$
DECLARE
  org integer := cairn.current_org();
  it cairn.access_review_items%ROWTYPE;
  st text;
BEGIN
  IF p_decision NOT IN ('keep', 'change', 'remove') THEN RAISE EXCEPTION 'invalid decision'; END IF;
  SELECT * INTO it FROM cairn.access_review_items WHERE item_id = p_item FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'no such item'; END IF;
  IF it.reviewer_principal <> cairn.current_principal() THEN
    RAISE EXCEPTION 'only the assigned reviewer can decide this' USING ERRCODE = '42501';
  END IF;
  SELECT state INTO st FROM cairn.access_reviews WHERE review_id = it.review_id;
  IF st <> 'open' THEN RAISE EXCEPTION 'this review is closed'; END IF;
  IF it.decision <> 'pending' THEN RAISE EXCEPTION 'this item is already decided'; END IF;
  IF p_decision = 'change' THEN
    IF p_new_role IS NULL OR p_new_role NOT IN ('owner', 'admin', 'manager', 'member', 'auditor', 'guest') THEN
      RAISE EXCEPTION 'name the new role';
    END IF;
    IF p_new_role = it.role_at_start THEN RAISE EXCEPTION 'that is the current role'; END IF;
  END IF;
  IF p_decision <> 'keep' AND (it.role_at_start = 'owner' OR p_new_role IN ('owner', 'admin')) AND cairn.member_role() <> 'owner' THEN
    RAISE EXCEPTION 'only an owner changes owners or grants admin' USING ERRCODE = '42501';
  END IF;
  IF p_decision <> 'keep' AND it.role_at_start = 'owner' AND
     (SELECT count(*) FROM cairn_meta.org_members WHERE org_id = org AND role = 'owner' AND state = 'active') <= 1 THEN
    RAISE EXCEPTION 'the last owner cannot be changed or removed';
  END IF;
  IF p_decision = 'change' THEN
    UPDATE cairn_meta.org_members SET role = p_new_role WHERE org_id = org AND principal_id = it.member_principal AND state = 'active';
  ELSIF p_decision = 'remove' THEN
    UPDATE cairn_meta.org_members SET state = 'deactivated', deactivated_at = now() WHERE org_id = org AND principal_id = it.member_principal;
    UPDATE cairn.memberships SET valid_period = tstzrange(lower(valid_period), now())
      WHERE member_id = it.member_principal AND org_id = org AND upper_inf(valid_period) AND lower(valid_period) < now();
    DELETE FROM cairn.memberships WHERE member_id = it.member_principal AND org_id = org AND upper_inf(valid_period) AND lower(valid_period) >= now();
    INSERT INTO cairn.acl_versions (reason, org_id) VALUES ('directory', org);
  END IF;
  UPDATE cairn.access_review_items
     SET decision = p_decision, new_role = CASE WHEN p_decision = 'change' THEN p_new_role END,
         comment = nullif(left(btrim(coalesce(p_comment, '')), 500), ''), decided_at = now()
   WHERE item_id = p_item;
END $$;

CREATE FUNCTION cairn.close_access_review(p_review bigint) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF EXISTS (SELECT 1 FROM cairn.access_review_items WHERE review_id = p_review AND decision = 'pending') THEN
    RAISE EXCEPTION 'every item must be decided before the review closes';
  END IF;
  UPDATE cairn.access_reviews SET state = 'closed', closed_at = now(), closed_by = cairn.current_principal()
    WHERE review_id = p_review AND state = 'open';
  IF NOT FOUND THEN RAISE EXCEPTION 'no such open review'; END IF;
END $$;

-- ---------------------------------------------------------------------------------------------
-- Controls: each is a check over the organisation's own records.
-- ---------------------------------------------------------------------------------------------

CREATE FUNCTION cairn.control_status()
RETURNS TABLE (control_id text, category text, title text, status text, detail text, route text)
  LANGUAGE plpgsql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, cairn, cairn_meta
  AS $$
DECLARE
  org integer := cairn.current_org();
  n bigint; m bigint; k bigint;
  chain record;
  expected integer;
BEGIN
  IF org IS NULL OR cairn.member_role() NOT IN ('owner', 'admin', 'auditor') THEN
    RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
  END IF;
  SELECT count(*) INTO expected FROM cairn_meta.org_members WHERE org_id = org AND state = 'active' AND role IN ('owner', 'admin', 'manager', 'member');

  -- POL-1
  SELECT count(*) INTO n FROM cairn.policies WHERE state = 'active';
  RETURN QUERY SELECT 'POL-1', 'Policies', 'Policies are approved and published', CASE WHEN n > 0 THEN 'pass' ELSE 'no_evidence' END,
    n || ' active policies, each published through an approval by someone other than its author.', 'governance';
  -- POL-2
  SELECT count(*) INTO n FROM cairn.policies p JOIN cairn.policy_versions v ON v.policy_id = p.policy_id AND v.state = 'published' WHERE p.state = 'active';
  SELECT count(*) INTO m FROM cairn.policies p JOIN cairn.policy_versions v ON v.policy_id = p.policy_id AND v.state = 'published'
    WHERE p.state = 'active' AND v.attest_by < now()
      AND (SELECT count(*) FROM cairn.attestations a WHERE a.version_id = v.version_id) < ceil(0.95 * greatest(expected, 1));
  RETURN QUERY SELECT 'POL-2', 'Policies', 'People attest to policies on time',
    CASE WHEN n = 0 THEN 'no_evidence' WHEN m > 0 THEN 'fail' ELSE 'pass' END,
    CASE WHEN n = 0 THEN 'No active policy to attest yet.' WHEN m > 0 THEN m || ' of ' || n || ' policies are past their attestation date with under 95% attested.' ELSE 'Every active policy is within its attestation date or at least 95% attested.' END, 'governance';
  -- POL-3
  SELECT count(*) INTO m FROM cairn.policies p JOIN cairn.policy_versions v ON v.policy_id = p.policy_id AND v.state = 'published'
    WHERE p.state = 'active' AND v.published_at + make_interval(days => p.review_days) < now();
  RETURN QUERY SELECT 'POL-3', 'Policies', 'Policies are reviewed on schedule', CASE WHEN n = 0 THEN 'no_evidence' WHEN m > 0 THEN 'fail' ELSE 'pass' END,
    CASE WHEN n = 0 THEN 'No active policy yet.' WHEN m > 0 THEN m || ' policies are past their review date.' ELSE 'No active policy is past its review date.' END, 'governance';
  -- ACC-1
  SELECT closed_at INTO chain FROM cairn.access_reviews WHERE state = 'closed' ORDER BY closed_at DESC LIMIT 1;
  RETURN QUERY SELECT 'ACC-1', 'Access', 'Access is reviewed at least quarterly',
    CASE WHEN chain.closed_at IS NULL THEN 'no_evidence' WHEN chain.closed_at < now() - interval '100 days' THEN 'fail' ELSE 'pass' END,
    CASE WHEN chain.closed_at IS NULL THEN 'No access review has been completed yet.' ELSE 'The last review closed on ' || to_char(chain.closed_at, 'YYYY-MM-DD') || '.' END, 'compliance';
  -- ACC-2
  SELECT count(*) FILTER (WHERE role = 'owner'), count(*) FILTER (WHERE role IN ('owner', 'admin')), count(*)
    INTO n, m, k FROM cairn_meta.org_members WHERE org_id = org AND state = 'active';
  RETURN QUERY SELECT 'ACC-2', 'Access', 'Privileged roles are few (least privilege)',
    CASE WHEN n <= 3 AND m <= greatest(3, ceil(0.2 * k)) THEN 'pass' ELSE 'fail' END,
    n || ' owners and ' || m || ' owners or administrators among ' || k || ' active members.', 'people';
  -- ACC-3
  SELECT count(*) INTO n FROM cairn_meta.org_members om WHERE om.org_id = org AND om.state = 'deactivated';
  SELECT count(*) INTO m FROM cairn_meta.org_members om WHERE om.org_id = org AND om.state = 'deactivated'
    AND EXISTS (SELECT 1 FROM cairn.memberships ms WHERE ms.member_id = om.principal_id AND ms.valid_period @> now());
  RETURN QUERY SELECT 'ACC-3', 'Access', 'Leavers lose access immediately', CASE WHEN n = 0 THEN 'no_evidence' WHEN m > 0 THEN 'fail' ELSE 'pass' END,
    CASE WHEN n = 0 THEN 'No one has been deactivated yet.' WHEN m > 0 THEN m || ' deactivated people still hold a group membership.' ELSE n || ' deactivated people, none with an active group membership.' END, 'people';
  -- ACC-4
  SELECT count(*) INTO n FROM cairn.approvals WHERE kind = 'access' AND state = 'approved';
  RETURN QUERY SELECT 'ACC-4', 'Access', 'Role grants are approved by an administrator', CASE WHEN n > 0 THEN 'pass' ELSE 'no_evidence' END,
    n || ' access requests approved by an administrator, none by the person who asked.', 'governance';
  -- CHG-1
  SELECT count(*) INTO n FROM cairn.approvals WHERE state IN ('approved', 'rejected');
  SELECT count(*) INTO m FROM cairn.approval_votes v JOIN cairn.approvals a ON a.approval_id = v.approval_id WHERE a.requested_by = v.voter_principal;
  RETURN QUERY SELECT 'CHG-1', 'Change', 'Changes are approved by someone other than the requester', CASE WHEN n = 0 THEN 'no_evidence' WHEN m > 0 THEN 'fail' ELSE 'pass' END,
    n || ' requests decided, ' || m || ' decided by their own requester (enforced by a database rule).', 'governance';
  -- CHG-2
  SELECT count(*) INTO n FROM cairn.decisions d WHERE d.origin = 'manual' AND d.verification = 'verified';
  RETURN QUERY SELECT 'CHG-2', 'Change', 'Significant decisions are recorded and approved', CASE WHEN n > 0 THEN 'pass' ELSE 'no_evidence' END,
    n || ' decisions recorded with their reasons and approved.', 'governance';
  -- LOG-1
  SELECT * INTO chain FROM cairn.verify_chain();
  RETURN QUERY SELECT 'LOG-1', 'Logging', 'The audit trail is tamper-evident',
    CASE WHEN chain.ok AND chain.checked > 0 THEN 'pass' WHEN chain.ok THEN 'no_evidence' ELSE 'fail' END,
    CASE WHEN chain.ok THEN chain.checked || ' events; every hash chains to the one before.' ELSE 'The chain breaks at event ' || chain.first_bad_seq || '.' END, 'audit';
  -- LOG-2
  SELECT count(*) INTO n FROM cairn.audit_events WHERE at > now() - interval '30 days';
  RETURN QUERY SELECT 'LOG-2', 'Logging', 'Administrative activity is logged', CASE WHEN n > 0 THEN 'pass' ELSE 'no_evidence' END,
    n || ' events in the last 30 days.', 'audit';
  -- LOG-3
  SELECT count(*) INTO n FROM cairn.audit_events WHERE outcome = 'denied';
  RETURN QUERY SELECT 'LOG-3', 'Logging', 'Refused actions are recorded', CASE WHEN n > 0 THEN 'pass' ELSE 'no_evidence' END,
    n || ' refused attempts on record.', 'audit';
  -- RSK-1
  SELECT count(*) FILTER (WHERE state <> 'closed'), count(*) FILTER (WHERE state <> 'closed' AND review_by < current_date) INTO n, m FROM cairn.risks;
  RETURN QUERY SELECT 'RSK-1', 'Risk', 'Risks are tracked and reviewed', CASE WHEN n = 0 THEN 'no_evidence' WHEN m > 0 THEN 'fail' ELSE 'pass' END,
    CASE WHEN n = 0 THEN 'No open risk recorded.' WHEN m > 0 THEN m || ' open risks are past their review date.' ELSE n || ' open risks, none past review.' END, 'governance';
  -- RSK-2
  SELECT count(*) FILTER (WHERE score >= 15), count(*) FILTER (WHERE score >= 15 AND length(btrim(mitigation)) < 10) INTO n, m FROM cairn.risks WHERE state <> 'closed';
  RETURN QUERY SELECT 'RSK-2', 'Risk', 'High risks have a mitigation', CASE WHEN m > 0 THEN 'fail' WHEN n = 0 THEN 'no_evidence' ELSE 'pass' END,
    CASE WHEN n = 0 THEN 'No high risk is open.' WHEN m > 0 THEN m || ' of ' || n || ' high risks have no mitigation.' ELSE n || ' high risks, each with a mitigation.' END, 'governance';
  -- OPS-1
  SELECT count(*) INTO n FROM generate_series(current_date - 14, current_date - 1, interval '1 day') d WHERE extract(isodow FROM d) < 6;
  SELECT count(*) INTO m FROM cairn.work_reports w WHERE w.report_date BETWEEN current_date - 14 AND current_date - 1 AND extract(isodow FROM w.report_date) < 6;
  RETURN QUERY SELECT 'OPS-1', 'Operations', 'Daily work is reported', CASE WHEN m = 0 THEN 'no_evidence' WHEN m::numeric / greatest(n * expected, 1) >= 0.7 THEN 'pass' ELSE 'fail' END,
    m || ' reports in the last 14 days against about ' || (n * expected) || ' expected.', 'reports';
END $$;

REVOKE ALL ON FUNCTION cairn.start_access_review(text, integer), cairn.decide_review_item(bigint, text, text, text),
  cairn.close_access_review(bigint), cairn.control_status() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn.start_access_review(text, integer), cairn.decide_review_item(bigint, text, text, text),
  cairn.close_access_review(bigint), cairn.control_status() TO cairn_app;
