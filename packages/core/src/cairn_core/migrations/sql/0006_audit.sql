-- Phase 1 audit log v1 (PD1-16): ids and hashes only, append-only for every role.
CREATE TABLE cairn.audit_log (
  audit_id          uuid PRIMARY KEY,
  at                timestamptz NOT NULL DEFAULT now(),
  endpoint          text NOT NULL CHECK (endpoint IN ('search', 'answer')),
  principal_id      integer   NOT NULL,
  acting_for        integer,
  principal_groups  integer[] NOT NULL,
  acl_version       bigint    NOT NULL,
  question_hmac     text NOT NULL CHECK (question_hmac ~ '^[0-9a-f]{64}$'),
  returned_item_ids bigint[]  NOT NULL,
  answer_sha256     text CHECK (answer_sha256 ~ '^[0-9a-f]{64}$'),
  outcome           text NOT NULL CHECK (outcome IN ('results', 'no_results', 'answered', 'not_found')),
  model             text
);

ALTER TABLE cairn.audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE cairn.audit_log FORCE ROW LEVEL SECURITY;

-- The API may insert a row for the caller (or for the user an agent acts for), and nothing
-- else: no SELECT (so INSERT must not use RETURNING), UPDATE or DELETE.
CREATE POLICY app_insert ON cairn.audit_log FOR INSERT TO cairn_app
  WITH CHECK (
    principal_id = nullif(current_setting('cairn.principal_id', true), '')::int
    OR acting_for = nullif(current_setting('cairn.principal_id', true), '')::int
  );
CREATE POLICY owner_read ON cairn.audit_log FOR SELECT TO cairn_owner USING (true);
CREATE POLICY owner_insert ON cairn.audit_log FOR INSERT TO cairn_owner WITH CHECK (true);

REVOKE ALL ON cairn.audit_log FROM PUBLIC;
GRANT INSERT ON cairn.audit_log TO cairn_app;

CREATE FUNCTION cairn.audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = '42501';
END $$;
REVOKE EXECUTE ON FUNCTION cairn.audit_log_append_only() FROM PUBLIC;

CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON cairn.audit_log
  FOR EACH ROW EXECUTE FUNCTION cairn.audit_log_append_only();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON cairn.audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION cairn.audit_log_append_only();

COMMENT ON TABLE cairn.audit_log IS 'Append-only. Holds HMACs, hashes and ids; never question or answer text.';
