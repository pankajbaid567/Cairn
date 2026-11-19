-- Citations of a claim: the sources of its fully visible support sets only (plan 2.11, 2.13).
-- A plain read of claim_support would be filtered row by row, so a set with one unreadable item would
-- look complete. This function evaluates each set as a whole, then returns only the items of sets the
-- caller can read in full.
CREATE FUNCTION cairn.claim_citations(p_claim bigint)
RETURNS TABLE (support_set integer, item_id bigint, external_id text, title text, source_ref text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT s.support_set, s.item_id, i.external_id, i.title, s.source_ref
  FROM cairn.claim_support s
  JOIN cairn.items i ON i.item_id = s.item_id
  WHERE s.claim_id = p_claim
    AND s.support_set IN (
      SELECT t.support_set FROM cairn.claim_support t
      WHERE t.claim_id = p_claim
      GROUP BY t.support_set
      HAVING bool_and(t.item_acl && nullif(current_setting('cairn.principal_groups', true), '')::int[])
    )
  ORDER BY s.support_set, s.item_id
$$;
REVOKE EXECUTE ON FUNCTION cairn.claim_citations(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn.claim_citations(bigint) TO cairn_app, cairn_owner, cairn_ingest;
