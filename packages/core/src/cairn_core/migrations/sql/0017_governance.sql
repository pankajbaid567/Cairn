-- Governance core (SaaS plan, Phase B): approvals with segregation of duties, decisions that are
-- proposed and approved inside Cairn, policies with versions and attestations, and a risk register.
--
-- Rules that live in the database, not only in the API:
--   * nobody votes on a request they made (segregation of duties), by trigger as well as function;
--   * a request needs `required` distinct approvals from eligible voters; one rejection ends it;
--   * a published policy version is never edited; a change is a new version that is approved again;
--   * an attestation is never changed or deleted;
--   * a decision's status changes only through an approved request, never by an UPDATE.

-- ---------------------------------------------------------------------------------------------
-- Approvals
-- ---------------------------------------------------------------------------------------------

CREATE TABLE cairn.approvals (
  approval_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind         text NOT NULL CHECK (kind IN ('decision', 'policy', 'access', 'exception')),
  subject_type text CHECK (subject_type IN ('decision', 'policy_version')),
  subject_id   bigint,
  title        text NOT NULL CHECK (length(btrim(title)) BETWEEN 3 AND 160),
  detail       jsonb NOT NULL DEFAULT '{}' CHECK (pg_column_size(detail) < 4096),
  requested_by integer NOT NULL,
  required     smallint NOT NULL DEFAULT 1 CHECK (required BETWEEN 1 AND 5),
  state        text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'approved', 'rejected', 'withdrawn')),
  expires_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  decided_at   timestamptz
);
SELECT cairn.tenant_table('cairn.approvals');
CREATE INDEX approvals_state_idx ON cairn.approvals (org_id, state, created_at DESC);

CREATE TABLE cairn.approval_votes (
  vote_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  approval_id    bigint NOT NULL,
  voter_principal integer NOT NULL,
  vote           text NOT NULL CHECK (vote IN ('approve', 'reject')),
  comment        text CHECK (length(comment) <= 500),
  voted_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (approval_id, voter_principal)
);
SELECT cairn.tenant_table('cairn.approval_votes');

CREATE FUNCTION cairn.approval_votes_sod() RETURNS trigger
  LANGUAGE plpgsql
  AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM cairn.approvals a WHERE a.approval_id = NEW.approval_id AND a.requested_by = NEW.voter_principal) THEN
    RAISE EXCEPTION 'segregation of duties: nobody votes on their own request' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER approval_votes_sod BEFORE INSERT ON cairn.approval_votes
  FOR EACH ROW EXECUTE FUNCTION cairn.approval_votes_sod();

CREATE FUNCTION cairn.immutable_row() RETURNS trigger
  LANGUAGE plpgsql
  AS $$ BEGIN RAISE EXCEPTION '% rows cannot be changed or deleted', TG_TABLE_NAME USING ERRCODE = '42501'; END $$;
CREATE TRIGGER approval_votes_immutable BEFORE UPDATE OR DELETE ON cairn.approval_votes
  FOR EACH ROW EXECUTE FUNCTION cairn.immutable_row();

-- Leads, auditors and the requester read an approval; members read their own requests.
CREATE POLICY app_read ON cairn.approvals FOR SELECT TO cairn_app
  USING (requested_by = cairn.current_principal() OR cairn.member_role() IN ('owner', 'admin', 'manager', 'auditor'));
CREATE POLICY app_read ON cairn.approval_votes FOR SELECT TO cairn_app
  USING (cairn.member_role() IN ('owner', 'admin', 'manager', 'auditor')
         OR voter_principal = cairn.current_principal()
         OR EXISTS (SELECT 1 FROM cairn.approvals a WHERE a.approval_id = approval_votes.approval_id));
REVOKE ALL ON cairn.approvals, cairn.approval_votes FROM PUBLIC;
GRANT SELECT ON cairn.approvals, cairn.approval_votes TO cairn_app;

-- Active members who may vote, other than the requester.
CREATE FUNCTION cairn.eligible_voters(p_requester integer, p_kind text) RETURNS integer
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, cairn, cairn_meta
  AS $$
  SELECT count(*)::integer FROM cairn_meta.org_members m
  WHERE m.org_id = cairn.current_org() AND m.state = 'active' AND m.principal_id <> p_requester
    AND m.role IN (CASE WHEN p_kind = 'access' THEN 'admin' ELSE 'manager' END, 'admin', 'owner')
$$;

-- ---------------------------------------------------------------------------------------------
-- Decisions proposed and approved inside Cairn
-- ---------------------------------------------------------------------------------------------

CREATE FUNCTION cairn.employees_group() RETURNS integer
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$ SELECT principal_id FROM cairn.principals WHERE org_id = cairn.current_org() AND external_id = 'group:employees' $$;

-- Move a decision to a new status: one event, the open version closed at now, a new open version.
CREATE FUNCTION cairn.decision_set_status(p_decision bigint, p_status text, p_note text, p_by text DEFAULT NULL)
RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE
  acl integer[] := ARRAY[cairn.employees_group()];
  seq integer;
  ev bigint;
  ts timestamptz := now();
BEGIN
  SELECT coalesce(max(e.seq), 0) + 1 INTO seq FROM cairn.decision_events e WHERE e.decision_id = p_decision;
  INSERT INTO cairn.decision_events (decision_id, seq, event, valid_at, recorded_at, note, by_key, acl, source_ref)
  VALUES (p_decision, seq, p_status, ts, ts, p_note, p_by, acl, 'cairn')
  RETURNING event_id INTO ev;
  UPDATE cairn.decision_versions SET valid_period = tstzrange(lower(valid_period), ts)
    WHERE decision_id = p_decision AND upper_inf(valid_period) AND upper_inf(recorded_period);
  INSERT INTO cairn.decision_versions (decision_id, status, valid_period, recorded_period, event_id, acl)
  VALUES (p_decision, p_status, tstzrange(ts, NULL), tstzrange(ts, NULL), ev, acl);
END $$;

CREATE FUNCTION cairn.propose_decision(p_title text, p_question text, p_outcome text, p_required integer)
RETURNS TABLE (decision_id bigint, key text, approval_id bigint)
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE
  who integer := cairn.current_principal();
  n integer;
  k text;
  d bigint;
  a bigint;
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin', 'manager', 'member') THEN
    RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
  END IF;
  IF length(btrim(p_title)) < 3 OR length(btrim(coalesce(p_outcome, ''))) < 3 THEN
    RAISE EXCEPTION 'a decision needs a title and an outcome';
  END IF;
  IF cairn.eligible_voters(who, 'decision') < p_required THEN
    RAISE EXCEPTION 'not enough approvers: this needs % people other than you who are managers or above', p_required;
  END IF;
  SELECT count(*) + 1 INTO n FROM cairn.decisions dd WHERE dd.origin = 'manual';
  k := 'DR-' || lpad(n::text, 3, '0');
  WHILE EXISTS (SELECT 1 FROM cairn.decisions dd WHERE dd.key = k) LOOP
    n := n + 1; k := 'DR-' || lpad(n::text, 3, '0');
  END LOOP;
  INSERT INTO cairn.decisions (key, title, question, outcome, origin, verification)
  VALUES (k, btrim(p_title), nullif(btrim(coalesce(p_question, '')), ''), btrim(p_outcome), 'manual', 'candidate')
  RETURNING cairn.decisions.decision_id INTO d;
  PERFORM cairn.decision_set_status(d, 'proposed', 'proposed in Cairn');
  INSERT INTO cairn.approvals (kind, subject_type, subject_id, title, detail, requested_by, required)
  VALUES ('decision', 'decision', d, left(btrim(p_title), 160), jsonb_build_object('key', k), who, p_required)
  RETURNING cairn.approvals.approval_id INTO a;
  RETURN QUERY SELECT d, k, a;
END $$;

-- The decisions proposed in Cairn, with their approval state. Decision content is visible to
-- every member of the organisation except guests, so this reads as the owner and filters here.
CREATE FUNCTION cairn.list_manual_decisions()
RETURNS TABLE (decision_id bigint, key text, title text, question text, outcome text, status text,
               created_at timestamptz, requested_by text, approval_id bigint, approval_state text,
               required smallint, approvals integer)
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
  SELECT d.decision_id, d.key, d.title, d.question, d.outcome,
         (SELECT v.status FROM cairn.decision_versions v WHERE v.decision_id = d.decision_id
            AND upper_inf(v.valid_period) AND upper_inf(v.recorded_period)),
         d.created_at, p.display_name, a.approval_id, a.state, a.required,
         (SELECT count(*)::integer FROM cairn.approval_votes av WHERE av.approval_id = a.approval_id AND av.vote = 'approve')
  FROM cairn.decisions d
  LEFT JOIN cairn.approvals a ON a.subject_type = 'decision' AND a.subject_id = d.decision_id
  LEFT JOIN cairn.principals p ON p.principal_id = a.requested_by
  WHERE d.origin = 'manual' AND d.org_id = cairn.current_org()
    AND cairn.member_role() IN ('owner', 'admin', 'manager', 'member', 'auditor')
  ORDER BY d.decision_id DESC
$$;
REVOKE ALL ON FUNCTION cairn.list_manual_decisions() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn.list_manual_decisions() TO cairn_app;

-- ---------------------------------------------------------------------------------------------
-- Policies
-- ---------------------------------------------------------------------------------------------

CREATE TABLE cairn.policies (
  policy_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug            text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,60}$'),
  title           text NOT NULL CHECK (length(btrim(title)) BETWEEN 3 AND 160),
  owner_principal integer NOT NULL,
  review_days     integer NOT NULL DEFAULT 365 CHECK (review_days BETWEEN 30 AND 1095),
  attest_days     integer NOT NULL DEFAULT 30 CHECK (attest_days BETWEEN 1 AND 180),
  state           text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'active', 'retired')),
  current_version integer,
  created_at      timestamptz NOT NULL DEFAULT now()
);
SELECT cairn.tenant_table('cairn.policies');
CREATE UNIQUE INDEX policies_slug_idx ON cairn.policies (org_id, slug);

CREATE TABLE cairn.policy_versions (
  version_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  policy_id    bigint  NOT NULL,
  version      integer NOT NULL CHECK (version >= 1),
  title        text    NOT NULL,
  body         text    NOT NULL CHECK (length(body) BETWEEN 10 AND 30000),
  summary      text    CHECK (length(summary) <= 500),
  state        text    NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'pending', 'published', 'superseded')),
  created_by   integer NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  attest_by    timestamptz,
  approval_id  bigint,
  UNIQUE (policy_id, version)
);
SELECT cairn.tenant_table('cairn.policy_versions');
CREATE UNIQUE INDEX policy_versions_published_idx ON cairn.policy_versions (policy_id) WHERE state = 'published';

-- A version's text is fixed once it leaves draft: only its state and dates may change.
CREATE FUNCTION cairn.policy_versions_guard() RETURNS trigger
  LANGUAGE plpgsql
  AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.state <> 'draft' THEN RAISE EXCEPTION 'only a draft can be deleted' USING ERRCODE = '42501'; END IF;
    RETURN OLD;
  END IF;
  IF OLD.state <> 'draft' AND (NEW.body <> OLD.body OR NEW.title <> OLD.title OR NEW.version <> OLD.version) THEN
    RAISE EXCEPTION 'a submitted policy version cannot be edited; write a new version' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER policy_versions_guard BEFORE UPDATE OR DELETE ON cairn.policy_versions
  FOR EACH ROW EXECUTE FUNCTION cairn.policy_versions_guard();

CREATE TABLE cairn.attestations (
  attestation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_id     bigint  NOT NULL,
  principal_id   integer NOT NULL,
  attested_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (version_id, principal_id)
);
SELECT cairn.tenant_table('cairn.attestations');
CREATE TRIGGER attestations_immutable BEFORE UPDATE OR DELETE ON cairn.attestations
  FOR EACH ROW EXECUTE FUNCTION cairn.immutable_row();

CREATE POLICY app_read ON cairn.policies FOR SELECT TO cairn_app
  USING (cairn.member_role() IN ('owner', 'admin', 'manager', 'member', 'auditor')
         AND (state <> 'draft' OR cairn.member_role() IN ('owner', 'admin') OR owner_principal = cairn.current_principal()));
CREATE POLICY app_read ON cairn.policy_versions FOR SELECT TO cairn_app
  USING (cairn.member_role() IN ('owner', 'admin', 'manager', 'member', 'auditor')
         AND (state IN ('published', 'superseded') OR cairn.member_role() IN ('owner', 'admin', 'auditor')
              OR created_by = cairn.current_principal()));
CREATE POLICY app_read ON cairn.attestations FOR SELECT TO cairn_app
  USING (principal_id = cairn.current_principal() OR cairn.member_role() IN ('owner', 'admin', 'manager', 'auditor'));
REVOKE ALL ON cairn.policies, cairn.policy_versions, cairn.attestations FROM PUBLIC;
GRANT SELECT ON cairn.policies, cairn.policy_versions, cairn.attestations TO cairn_app;

CREATE FUNCTION cairn.create_policy(p_slug text, p_title text, p_body text, p_review_days integer, p_attest_days integer)
RETURNS bigint
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE p bigint;
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  INSERT INTO cairn.policies (slug, title, owner_principal, review_days, attest_days)
  VALUES (p_slug, btrim(p_title), cairn.current_principal(), p_review_days, p_attest_days) RETURNING policy_id INTO p;
  INSERT INTO cairn.policy_versions (policy_id, version, title, body, created_by)
  VALUES (p, 1, btrim(p_title), p_body, cairn.current_principal());
  RETURN p;
END $$;

-- Save the draft of the next version (creating it when the latest version is already submitted).
CREATE FUNCTION cairn.save_policy_draft(p_policy bigint, p_title text, p_body text, p_summary text) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE
  lastv record;
  v integer;
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  SELECT * INTO lastv FROM cairn.policy_versions pv WHERE pv.policy_id = p_policy ORDER BY pv.version DESC LIMIT 1;
  IF NOT FOUND THEN RAISE EXCEPTION 'no such policy'; END IF;
  IF lastv.state = 'draft' THEN
    UPDATE cairn.policy_versions SET title = btrim(p_title), body = p_body, summary = left(p_summary, 500)
      WHERE version_id = lastv.version_id;
    v := lastv.version;
  ELSIF lastv.state = 'pending' THEN
    RAISE EXCEPTION 'a version is waiting for approval';
  ELSE
    v := lastv.version + 1;
    INSERT INTO cairn.policy_versions (policy_id, version, title, body, summary, created_by)
    VALUES (p_policy, v, btrim(p_title), p_body, left(p_summary, 500), cairn.current_principal());
  END IF;
  RETURN v;
END $$;

CREATE FUNCTION cairn.request_approval(
  p_kind text, p_title text, p_detail jsonb, p_required integer, p_subject_type text, p_subject_id bigint, p_expires timestamptz
) RETURNS bigint
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn, cairn_meta
  AS $$
DECLARE
  who integer := cairn.current_principal();
  r text := cairn.member_role();
  a bigint;
BEGIN
  IF p_kind = 'policy' AND r NOT IN ('owner', 'admin') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF p_kind = 'exception' AND r NOT IN ('owner', 'admin', 'manager') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF p_kind = 'access' AND r NOT IN ('member', 'manager', 'auditor') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  IF p_kind = 'decision' THEN RAISE EXCEPTION 'decisions are proposed with propose_decision'; END IF;
  IF p_kind = 'access' AND coalesce(p_detail->>'role', '') NOT IN ('manager', 'member', 'auditor') THEN
    RAISE EXCEPTION 'an access request names manager, member or auditor';
  END IF;
  IF p_kind = 'access' AND p_detail->>'role' = r THEN RAISE EXCEPTION 'you already have that role'; END IF;
  IF cairn.eligible_voters(who, p_kind) < p_required THEN
    RAISE EXCEPTION 'not enough approvers: this needs % people other than you', p_required;
  END IF;
  INSERT INTO cairn.approvals (kind, subject_type, subject_id, title, detail, requested_by, required, expires_at)
  VALUES (p_kind, p_subject_type, p_subject_id, left(btrim(p_title), 160), coalesce(p_detail, '{}'), who, p_required, p_expires)
  RETURNING approval_id INTO a;
  RETURN a;
END $$;

CREATE FUNCTION cairn.submit_policy(p_policy bigint) RETURNS bigint
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE
  pv record;
  a bigint;
BEGIN
  SELECT * INTO pv FROM cairn.policy_versions v WHERE v.policy_id = p_policy AND v.state = 'draft' ORDER BY v.version DESC LIMIT 1;
  IF NOT FOUND THEN RAISE EXCEPTION 'there is no draft to submit'; END IF;
  a := cairn.request_approval('policy', 'Policy: ' || pv.title || ' v' || pv.version, jsonb_build_object('policy_id', p_policy, 'version', pv.version), 1, 'policy_version', pv.version_id, NULL);
  UPDATE cairn.policy_versions SET state = 'pending', approval_id = a WHERE version_id = pv.version_id;
  RETURN a;
END $$;

-- What an approved or rejected request changes.
CREATE FUNCTION cairn.apply_approval(p_approval bigint) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn, cairn_meta
  AS $$
DECLARE
  a cairn.approvals%ROWTYPE;
  pv record;
  days integer;
BEGIN
  SELECT * INTO a FROM cairn.approvals WHERE approval_id = p_approval;
  IF a.kind = 'decision' THEN
    PERFORM cairn.decision_set_status(a.subject_id, CASE a.state WHEN 'approved' THEN 'accepted' WHEN 'rejected' THEN 'rejected' ELSE 'withdrawn' END,
      'approval ' || a.state);
    UPDATE cairn.decisions SET verification = CASE a.state WHEN 'approved' THEN 'verified' ELSE 'rejected' END
      WHERE decision_id = a.subject_id;
  ELSIF a.kind = 'policy' THEN
    SELECT * INTO pv FROM cairn.policy_versions WHERE version_id = a.subject_id;
    IF a.state = 'approved' THEN
      SELECT attest_days INTO days FROM cairn.policies WHERE policy_id = pv.policy_id;
      UPDATE cairn.policy_versions SET state = 'superseded' WHERE policy_id = pv.policy_id AND state = 'published';
      UPDATE cairn.policy_versions SET state = 'published', published_at = now(), attest_by = now() + make_interval(days => days)
        WHERE version_id = pv.version_id;
      UPDATE cairn.policies SET state = 'active', current_version = pv.version WHERE policy_id = pv.policy_id;
    ELSE
      UPDATE cairn.policy_versions SET state = 'draft' WHERE version_id = pv.version_id;
    END IF;
  ELSIF a.kind = 'access' AND a.state = 'approved' THEN
    UPDATE cairn_meta.org_members SET role = a.detail->>'role'
      WHERE org_id = cairn.current_org() AND principal_id = a.requested_by AND state = 'active';
  END IF;
END $$;

CREATE FUNCTION cairn.cast_vote(p_approval bigint, p_vote text, p_comment text) RETURNS text
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE
  who integer := cairn.current_principal();
  r text := cairn.member_role();
  a cairn.approvals%ROWTYPE;
  yes integer;
BEGIN
  IF p_vote NOT IN ('approve', 'reject') THEN RAISE EXCEPTION 'invalid vote'; END IF;
  SELECT * INTO a FROM cairn.approvals WHERE approval_id = p_approval FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'no such request'; END IF;
  IF a.state <> 'pending' THEN RAISE EXCEPTION 'this request is already %', a.state; END IF;
  IF a.expires_at IS NOT NULL AND a.expires_at < now() THEN RAISE EXCEPTION 'this request has expired'; END IF;
  IF a.requested_by = who THEN RAISE EXCEPTION 'segregation of duties: nobody votes on their own request' USING ERRCODE = '42501'; END IF;
  IF r NOT IN ('owner', 'admin') AND NOT (r = 'manager' AND a.kind <> 'access') THEN
    RAISE EXCEPTION 'your role cannot vote on this request' USING ERRCODE = '42501';
  END IF;
  INSERT INTO cairn.approval_votes (approval_id, voter_principal, vote, comment)
  VALUES (p_approval, who, p_vote, nullif(left(btrim(coalesce(p_comment, '')), 500), ''));
  IF p_vote = 'reject' THEN
    UPDATE cairn.approvals SET state = 'rejected', decided_at = now() WHERE approval_id = p_approval;
  ELSE
    SELECT count(*) INTO yes FROM cairn.approval_votes WHERE approval_id = p_approval AND vote = 'approve';
    IF yes >= a.required THEN
      UPDATE cairn.approvals SET state = 'approved', decided_at = now() WHERE approval_id = p_approval;
    END IF;
  END IF;
  IF (SELECT state FROM cairn.approvals WHERE approval_id = p_approval) <> 'pending' THEN
    PERFORM cairn.apply_approval(p_approval);
  END IF;
  RETURN (SELECT state FROM cairn.approvals WHERE approval_id = p_approval);
END $$;

CREATE FUNCTION cairn.withdraw_approval(p_approval bigint) RETURNS boolean
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE a cairn.approvals%ROWTYPE;
BEGIN
  SELECT * INTO a FROM cairn.approvals WHERE approval_id = p_approval FOR UPDATE;
  IF NOT FOUND OR a.state <> 'pending' THEN RETURN false; END IF;
  IF a.requested_by <> cairn.current_principal() AND cairn.member_role() NOT IN ('owner', 'admin') THEN
    RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
  END IF;
  UPDATE cairn.approvals SET state = 'withdrawn', decided_at = now() WHERE approval_id = p_approval;
  PERFORM cairn.apply_approval(p_approval);
  RETURN true;
END $$;

CREATE FUNCTION cairn.mark_decision_implemented(p_decision bigint, p_note text) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE st text;
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin', 'manager') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  SELECT v.status INTO st FROM cairn.decision_versions v
    WHERE v.decision_id = p_decision AND upper_inf(v.valid_period) AND upper_inf(v.recorded_period);
  IF st IS DISTINCT FROM 'accepted' THEN RAISE EXCEPTION 'only an accepted decision can be marked implemented'; END IF;
  PERFORM cairn.decision_set_status(p_decision, 'implemented', left(coalesce(p_note, 'implemented'), 300));
END $$;

CREATE FUNCTION cairn.attest_policy(p_policy bigint) RETURNS boolean
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
DECLARE v bigint;
BEGIN
  IF cairn.member_role() NOT IN ('owner', 'admin', 'manager', 'member') THEN RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501'; END IF;
  SELECT version_id INTO v FROM cairn.policy_versions WHERE policy_id = p_policy AND state = 'published';
  IF NOT FOUND THEN RAISE EXCEPTION 'this policy has no published version'; END IF;
  INSERT INTO cairn.attestations (version_id, principal_id) VALUES (v, cairn.current_principal())
    ON CONFLICT (version_id, principal_id) DO NOTHING;
  RETURN FOUND;
END $$;

-- ---------------------------------------------------------------------------------------------
-- Risk register
-- ---------------------------------------------------------------------------------------------

CREATE TABLE cairn.risks (
  risk_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title           text NOT NULL CHECK (length(btrim(title)) BETWEEN 3 AND 160),
  description     text NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
  category        text NOT NULL DEFAULT 'operational' CHECK (category IN ('security', 'operational', 'compliance', 'financial', 'people', 'vendor')),
  likelihood      smallint NOT NULL CHECK (likelihood BETWEEN 1 AND 5),
  impact          smallint NOT NULL CHECK (impact BETWEEN 1 AND 5),
  score           smallint GENERATED ALWAYS AS (likelihood * impact) STORED,
  owner_principal integer NOT NULL,
  state           text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'mitigating', 'accepted', 'closed')),
  mitigation      text NOT NULL DEFAULT '' CHECK (length(mitigation) <= 2000),
  review_by       date,
  created_by      integer NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
SELECT cairn.tenant_table('cairn.risks');
CREATE FUNCTION cairn.risks_touch() RETURNS trigger LANGUAGE plpgsql
  AS $$ BEGIN NEW.updated_at := now(); RETURN NEW; END $$;
CREATE TRIGGER risks_touch BEFORE UPDATE ON cairn.risks FOR EACH ROW EXECUTE FUNCTION cairn.risks_touch();
CREATE POLICY app_read ON cairn.risks FOR SELECT TO cairn_app
  USING (cairn.member_role() IN ('owner', 'admin', 'manager', 'auditor'));
CREATE POLICY app_insert ON cairn.risks FOR INSERT TO cairn_app
  WITH CHECK (cairn.member_role() IN ('owner', 'admin', 'manager') AND created_by = cairn.current_principal());
CREATE POLICY app_update ON cairn.risks FOR UPDATE TO cairn_app
  USING (cairn.member_role() IN ('owner', 'admin') OR owner_principal = cairn.current_principal())
  WITH CHECK (cairn.member_role() IN ('owner', 'admin') OR owner_principal = cairn.current_principal());
REVOKE ALL ON cairn.risks FROM PUBLIC;
GRANT SELECT, INSERT ON cairn.risks TO cairn_app;
GRANT UPDATE (title, description, category, likelihood, impact, owner_principal, state, mitigation, review_by) ON cairn.risks TO cairn_app;

REVOKE ALL ON FUNCTION cairn.eligible_voters(integer, text), cairn.employees_group(),
  cairn.decision_set_status(bigint, text, text, text), cairn.propose_decision(text, text, text, integer),
  cairn.create_policy(text, text, text, integer, integer), cairn.save_policy_draft(bigint, text, text, text),
  cairn.request_approval(text, text, jsonb, integer, text, bigint, timestamptz), cairn.submit_policy(bigint),
  cairn.apply_approval(bigint), cairn.cast_vote(bigint, text, text), cairn.withdraw_approval(bigint),
  cairn.mark_decision_implemented(bigint, text), cairn.attest_policy(bigint) FROM PUBLIC;
-- Internal helpers (decision_set_status, apply_approval, eligible_voters, employees_group) are not granted.
GRANT EXECUTE ON FUNCTION cairn.propose_decision(text, text, text, integer),
  cairn.create_policy(text, text, text, integer, integer), cairn.save_policy_draft(bigint, text, text, text),
  cairn.request_approval(text, text, jsonb, integer, text, bigint, timestamptz), cairn.submit_policy(bigint),
  cairn.cast_vote(bigint, text, text), cairn.withdraw_approval(bigint),
  cairn.mark_decision_implemented(bigint, text), cairn.attest_policy(bigint) TO cairn_app;
