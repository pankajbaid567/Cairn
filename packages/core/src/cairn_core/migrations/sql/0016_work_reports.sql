-- Daily work reports (SaaS plan, Phase C).
--
-- Who may read a report is decided here, in the database, like every other content table:
--   the author; owners and admins (everyone's); managers and members (their teammates', that is
--   people who share an active team with them). Auditors and guests read only their own, because
--   a report is content. A report cannot be written for a future day or for a day more than 14
--   days back, and it cannot be deleted, so the record of who reported what, when, stays whole.

CREATE FUNCTION cairn.shares_team(a integer, b integer) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
  SELECT EXISTS (
    SELECT 1 FROM cairn.memberships ma
    JOIN cairn.memberships mb ON mb.group_id = ma.group_id
    JOIN cairn.principals t ON t.principal_id = ma.group_id AND t.kind = 'team'
    WHERE ma.member_id = a AND mb.member_id = b
      AND ma.valid_period @> now() AND mb.valid_period @> now()
  )
$$;

-- The principals the caller shares an active team with, themselves excluded.
CREATE FUNCTION cairn.teammates() RETURNS SETOF integer
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, cairn
  AS $$
  SELECT DISTINCT mb.member_id FROM cairn.memberships ma
  JOIN cairn.memberships mb ON mb.group_id = ma.group_id AND mb.member_id <> ma.member_id
  JOIN cairn.principals t ON t.principal_id = ma.group_id AND t.kind = 'team'
  WHERE ma.member_id = cairn.current_principal() AND ma.valid_period @> now() AND mb.valid_period @> now()
$$;

CREATE FUNCTION cairn.report_readable(p_author integer) RETURNS boolean
  LANGUAGE sql STABLE
  AS $$
  SELECT p_author = cairn.current_principal()
      OR cairn.member_role() IN ('owner', 'admin')
      OR (cairn.member_role() IN ('manager', 'member') AND cairn.shares_team(cairn.current_principal(), p_author))
$$;

CREATE TABLE cairn.work_reports (
  report_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  author_principal integer NOT NULL,
  report_date      date    NOT NULL,
  done             text    NOT NULL DEFAULT '' CHECK (length(done) <= 4000),
  next_up          text    NOT NULL DEFAULT '' CHECK (length(next_up) <= 2000),
  blockers         text    NOT NULL DEFAULT '' CHECK (length(blockers) <= 2000),
  links            jsonb   NOT NULL DEFAULT '[]'
                   CHECK (jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 20),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (length(btrim(done)) > 0 OR length(btrim(blockers)) > 0)
);
SELECT cairn.tenant_table('cairn.work_reports');
CREATE UNIQUE INDEX work_reports_author_day_idx ON cairn.work_reports (org_id, author_principal, report_date);
CREATE INDEX work_reports_day_idx ON cairn.work_reports (org_id, report_date DESC);

CREATE FUNCTION cairn.work_reports_guard() RETURNS trigger
  LANGUAGE plpgsql
  AS $$
BEGIN
  IF session_user NOT IN ('cairn_owner', 'cairn_ingest') THEN
    IF NEW.report_date > current_date OR NEW.report_date < current_date - 14 THEN
      RAISE EXCEPTION 'a report can be written for today or the last 14 days only' USING ERRCODE = '22023';
    END IF;
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.author_principal <> OLD.author_principal OR NEW.report_date <> OLD.report_date THEN
      RAISE EXCEPTION 'the author and the day of a report cannot change' USING ERRCODE = '42501';
    END IF;
    IF session_user NOT IN ('cairn_owner', 'cairn_ingest') AND OLD.report_date < current_date - 14 THEN
      RAISE EXCEPTION 'this report is closed' USING ERRCODE = '42501';
    END IF;
    NEW.created_at := OLD.created_at;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER work_reports_guard BEFORE INSERT OR UPDATE ON cairn.work_reports
  FOR EACH ROW EXECUTE FUNCTION cairn.work_reports_guard();

CREATE POLICY app_read ON cairn.work_reports FOR SELECT TO cairn_app
  USING (cairn.report_readable(author_principal));
CREATE POLICY app_insert ON cairn.work_reports FOR INSERT TO cairn_app
  WITH CHECK (author_principal = cairn.current_principal()
              AND cairn.member_role() IN ('owner', 'admin', 'manager', 'member'));
CREATE POLICY app_update ON cairn.work_reports FOR UPDATE TO cairn_app
  USING (author_principal = cairn.current_principal())
  WITH CHECK (author_principal = cairn.current_principal());
REVOKE ALL ON cairn.work_reports FROM PUBLIC;
GRANT SELECT, INSERT ON cairn.work_reports TO cairn_app;
GRANT UPDATE (done, next_up, blockers, links) ON cairn.work_reports TO cairn_app;

CREATE TABLE cairn.report_comments (
  comment_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  report_id        bigint  NOT NULL,
  author_principal integer NOT NULL,
  body             text    NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 1000),
  created_at       timestamptz NOT NULL DEFAULT now()
);
SELECT cairn.tenant_table('cairn.report_comments');
CREATE INDEX report_comments_report_idx ON cairn.report_comments (report_id);
-- A comment is visible exactly when its report is (the report's own policy applies in the subquery).
CREATE POLICY app_read ON cairn.report_comments FOR SELECT TO cairn_app
  USING (EXISTS (SELECT 1 FROM cairn.work_reports r WHERE r.report_id = report_comments.report_id));
CREATE POLICY app_insert ON cairn.report_comments FOR INSERT TO cairn_app
  WITH CHECK (author_principal = cairn.current_principal()
              AND cairn.member_role() IN ('owner', 'admin', 'manager', 'member')
              AND EXISTS (SELECT 1 FROM cairn.work_reports r WHERE r.report_id = report_comments.report_id));
REVOKE ALL ON cairn.report_comments FROM PUBLIC;
GRANT SELECT, INSERT ON cairn.report_comments TO cairn_app;

-- Digests the model wrote from the reports a person may read. A digest is private to whoever
-- asked for it, because it was built from what they could see and nobody else may share that view.
CREATE TABLE cairn.report_digests (
  digest_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_by   integer NOT NULL,
  scope        text    NOT NULL CHECK (scope IN ('mine', 'team', 'org')),
  team_id      integer,
  period_start date    NOT NULL,
  period_end   date    NOT NULL CHECK (period_end >= period_start AND period_end - period_start <= 31),
  input_hash   text    NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
  body         jsonb   NOT NULL,
  model        text    NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
SELECT cairn.tenant_table('cairn.report_digests');
CREATE UNIQUE INDEX report_digests_key_idx ON cairn.report_digests
  (org_id, created_by, scope, coalesce(team_id, 0), period_start, period_end, input_hash);
CREATE POLICY app_read ON cairn.report_digests FOR SELECT TO cairn_app USING (created_by = cairn.current_principal());
CREATE POLICY app_insert ON cairn.report_digests FOR INSERT TO cairn_app WITH CHECK (created_by = cairn.current_principal());
REVOKE ALL ON cairn.report_digests FROM PUBLIC;
GRANT SELECT, INSERT ON cairn.report_digests TO cairn_app;

REVOKE ALL ON FUNCTION cairn.shares_team(integer, integer), cairn.teammates(), cairn.report_readable(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn.shares_team(integer, integer), cairn.teammates(), cairn.report_readable(integer) TO cairn_app;

COMMENT ON TABLE cairn.work_reports IS 'One report per person per day. Readable by the author, administrators and teammates; never deleted.';
