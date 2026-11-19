-- "Access and audit" page (plan 3.14): a user reads their own audit entries, and only those.
-- The API role has no SELECT on the audit log (append-only, ids and hashes only). This function
-- reads the caller's id from the transaction setting the API writes from the session cookie, never
-- from a parameter, so it cannot be pointed at another person's rows.
CREATE FUNCTION cairn.my_audit(p_limit integer DEFAULT 50)
RETURNS TABLE (audit_id uuid, at timestamptz, endpoint text, outcome text, returned integer,
               acl_version bigint, model text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, cairn AS $$
  SELECT a.audit_id, a.at, a.endpoint, a.outcome, cardinality(a.returned_item_ids), a.acl_version, a.model
  FROM cairn.audit_log a
  WHERE a.principal_id = nullif(current_setting('cairn.principal_id', true), '')::int
     OR a.acting_for = nullif(current_setting('cairn.principal_id', true), '')::int
  ORDER BY a.at DESC
  LIMIT least(greatest(p_limit, 1), 200)
$$;
REVOKE EXECUTE ON FUNCTION cairn.my_audit(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cairn.my_audit(integer) TO cairn_app, cairn_owner;
