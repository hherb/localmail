-- Refresh-token family revocation (RFC 9700 §4.14.2). Rotation no longer
-- hard-deletes the presented token: it is tombstoned via consumed_at, and the
-- successor inherits the same family_id. Replaying a consumed token signals
-- theft, so the whole family is deleted (see mcp/oauth/refresh.py). The
-- client_id index serves clients.cleanup_unused's correlated NOT EXISTS (#185).

ALTER TABLE oauth_refresh_tokens
    ADD COLUMN family_id   UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN consumed_at TIMESTAMPTZ;

CREATE INDEX oauth_refresh_tokens_family_id_idx ON oauth_refresh_tokens (family_id);
CREATE INDEX oauth_refresh_tokens_client_id_idx ON oauth_refresh_tokens (client_id);
