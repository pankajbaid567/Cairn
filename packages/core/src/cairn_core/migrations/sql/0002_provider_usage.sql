-- Daily usage of metered providers (Workers AI neurons), reserved before each call.
-- Free-tier retarget FT-22, Requirements 15.5 and 15.6. The table is bookkeeping, not
-- content: it lives in schema cairn_meta, outside schema cairn and its row level security
-- rule, and holds no tenant data. Reservations are never released, so the app role needs
-- no DELETE.
CREATE TABLE cairn_meta.provider_usage (
  day              date        NOT NULL,
  provider         text        NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_]{0,31}$'),
  neurons_reserved bigint      NOT NULL DEFAULT 0 CHECK (neurons_reserved >= 0),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (day, provider)
);

REVOKE ALL ON cairn_meta.provider_usage FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON cairn_meta.provider_usage TO cairn_app;

COMMENT ON TABLE cairn_meta.provider_usage IS 'Provider budget reservations. Not content: outside schema cairn and its RLS rule.';
