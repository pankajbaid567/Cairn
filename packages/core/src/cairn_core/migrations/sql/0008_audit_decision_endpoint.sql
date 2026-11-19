-- Decision reads (page, timeline, graph) are audited like search and answer (plan 2.9).
ALTER TABLE cairn.audit_log DROP CONSTRAINT audit_log_endpoint_check;
ALTER TABLE cairn.audit_log ADD CONSTRAINT audit_log_endpoint_check CHECK (endpoint IN ('search', 'answer', 'decision'));
