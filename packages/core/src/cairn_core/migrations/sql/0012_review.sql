-- Conflicts and the review queue (plan 4.4, 4.5).
--
-- A conflict is two or more sources that disagree about one subject (for example two dates). It is
-- never resolved automatically (D4): it opens a review item, marks the decision disputed, and waits
-- for a person. A reader sees a conflict only if they can read EVERY side: seeing one side alone
-- would reveal that another, hidden, source disagrees. Resolution goes through a function that
-- checks visibility, requires a reason and records who resolved it.

ALTER TABLE cairn.review_items ADD COLUMN resolved_by integer;

CREATE TABLE cairn.conflict_sides (
  review_id  bigint  NOT NULL,
  side       integer NOT NULL,
  value      text    NOT NULL CHECK (length(value) BETWEEN 1 AND 200),
  tier       text    NOT NULL CHECK (tier IN ('T1', 'T2', 'T3', 'T4', 'T5')),
  source_ref text,
  item_id    bigint,
  item_acl   integer[] NOT NULL CHECK (cardinality(item_acl) > 0),
  PRIMARY KEY (review_id, side)
);
ALTER TABLE cairn.conflict_sides ENABLE ROW LEVEL SECURITY; ALTER TABLE cairn.conflict_sides FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_all ON cairn.conflict_sides FOR ALL TO cairn_owner USING (true) WITH CHECK (true);
CREATE POLICY ingest_all ON cairn.conflict_sides FOR ALL TO cairn_ingest USING (true) WITH CHECK (true);
REVOKE ALL ON cairn.conflict_sides FROM PUBLIC;
GRANT SELECT, INSERT ON cairn.conflict_sides TO cairn_ingest;
GRANT UPDATE (item_acl) ON cairn.conflict_sides TO cairn_ingest;
GRANT DELETE ON cairn.conflict_sides TO cairn_ingest;
GRANT INSERT ON cairn.review_items TO cairn_ingest;

CREATE FUNCTION cairn.conflict_visible(p_review bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT EXISTS (SELECT 1 FROM cairn.conflict_sides s WHERE s.review_id = p_review)
     AND NOT EXISTS (
       SELECT 1 FROM cairn.conflict_sides s
       WHERE s.review_id = p_review
         AND NOT (s.item_acl && nullif(current_setting('cairn.principal_groups', true), '')::int[]))
$$;

CREATE FUNCTION cairn.review_visible(p_review bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT CASE r.kind
           WHEN 'conflict' THEN cairn.conflict_visible(r.review_id)
           ELSE r.decision_id IS NOT NULL AND cairn.decision_visible(r.decision_id)
         END
  FROM cairn.review_items r WHERE r.review_id = p_review
$$;

CREATE FUNCTION cairn.list_reviews(p_state text DEFAULT 'open')
RETURNS TABLE (review_id bigint, kind text, decision_key text, state text, reason text,
               created_at timestamptz, resolved_at timestamptz, detail jsonb, sides jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT r.review_id, r.kind, d.key, r.state, r.reason, r.created_at, r.resolved_at, r.detail,
         CASE WHEN r.kind = 'conflict' THEN (
           SELECT jsonb_agg(jsonb_build_object('value', s.value, 'tier', s.tier, 'source', s.source_ref)
                            ORDER BY s.side)
           FROM cairn.conflict_sides s WHERE s.review_id = r.review_id) END
  FROM cairn.review_items r LEFT JOIN cairn.decisions d ON d.decision_id = r.decision_id
  WHERE cairn.review_visible(r.review_id) AND (p_state = 'all' OR r.state = p_state)
  ORDER BY r.created_at DESC, r.review_id DESC
  LIMIT 100
$$;

-- verify | edit | dispute | reject. The reason is required; an edit may carry replacement text
-- for the decision's title, question or outcome. Returns false when the item is not visible or is
-- already resolved, so a caller learns nothing about items they cannot see.
CREATE FUNCTION cairn.resolve_review(p_review bigint, p_state text, p_reason text, p_edit jsonb DEFAULT NULL)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
DECLARE
  who integer := nullif(current_setting('cairn.principal_id', true), '')::int;
  dec bigint;
BEGIN
  IF p_state NOT IN ('verified', 'edited', 'disputed', 'rejected') THEN
    RAISE EXCEPTION 'invalid state' USING ERRCODE = '22023';
  END IF;
  IF p_reason IS NULL OR length(btrim(p_reason)) < 5 OR length(p_reason) > 500 THEN
    RAISE EXCEPTION 'a reason of 5 to 500 characters is required' USING ERRCODE = '22023';
  END IF;
  IF who IS NULL OR NOT cairn.review_visible(p_review) THEN RETURN false; END IF;
  UPDATE cairn.review_items
     SET state = p_state, reason = btrim(p_reason), resolved_at = now(), resolved_by = who
   WHERE review_id = p_review AND state = 'open'
   RETURNING decision_id INTO dec;
  IF NOT FOUND THEN RETURN false; END IF;
  IF dec IS NOT NULL THEN
    UPDATE cairn.decisions SET
      verification = CASE p_state WHEN 'disputed' THEN 'disputed' WHEN 'rejected' THEN 'rejected' ELSE 'verified' END,
      title    = CASE WHEN p_state = 'edited' AND p_edit ? 'title'    THEN left(p_edit->>'title', 300) ELSE title END,
      question = CASE WHEN p_state = 'edited' AND p_edit ? 'question' THEN left(p_edit->>'question', 500) ELSE question END,
      outcome  = CASE WHEN p_state = 'edited' AND p_edit ? 'outcome'  THEN left(p_edit->>'outcome', 1000) ELSE outcome END
    WHERE decision_id = dec;
  END IF;
  RETURN true;
END
$$;

REVOKE EXECUTE ON FUNCTION cairn.conflict_visible(bigint), cairn.review_visible(bigint),
  cairn.list_reviews(text), cairn.resolve_review(bigint, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn.conflict_visible(bigint), cairn.review_visible(bigint),
  cairn.list_reviews(text), cairn.resolve_review(bigint, text, text, jsonb)
  TO cairn_app, cairn_owner, cairn_ingest;
