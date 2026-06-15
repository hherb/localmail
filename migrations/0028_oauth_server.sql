-- OAuth 2.1 authorization-server storage (MCP "Approach B"). Tokens/codes are
-- stored SHA-256-hashed; the raw value is returned to the client exactly once.
-- Access tokens reuse api_tokens; the new oauth_client_id column attributes an
-- OAuth-minted access token to its client and cascade-revokes with it. NULL on
-- every login-issued token, so existing rows + /v1/auth/login are unaffected.

CREATE TABLE oauth_clients (
    client_id                  TEXT PRIMARY KEY,
    client_secret_sha256       BYTEA,
    redirect_uris              TEXT[] NOT NULL,
    client_name                TEXT,
    grant_types                TEXT[],
    response_types             TEXT[],
    token_endpoint_auth_method TEXT,
    scope                      TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at               TIMESTAMPTZ
);

CREATE TABLE oauth_authorization_codes (
    code_sha256                      BYTEA PRIMARY KEY,
    client_id                        TEXT NOT NULL REFERENCES oauth_clients ON DELETE CASCADE,
    user_id                          BIGINT NOT NULL REFERENCES api_users ON DELETE CASCADE,
    redirect_uri                     TEXT NOT NULL,
    redirect_uri_provided_explicitly BOOLEAN NOT NULL,
    code_challenge                   TEXT NOT NULL,
    scopes                           TEXT[] NOT NULL DEFAULT '{}',
    expires_at                       TIMESTAMPTZ NOT NULL,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_refresh_tokens (
    token_sha256 BYTEA        PRIMARY KEY,
    client_id    TEXT         NOT NULL REFERENCES oauth_clients ON DELETE CASCADE,
    user_id      BIGINT       NOT NULL REFERENCES api_users ON DELETE CASCADE,
    scopes       TEXT[]       NOT NULL DEFAULT '{}',
    expires_at   TIMESTAMPTZ  NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

ALTER TABLE api_tokens
    ADD COLUMN oauth_client_id TEXT REFERENCES oauth_clients ON DELETE CASCADE;

-- Per-IP rate-limit audit for open Dynamic Client Registration. Append-only,
-- read by a sliding-window COUNT, swept on retention -- same shape and
-- multi-worker-safety rationale as api_login_attempts.
CREATE TABLE oauth_registration_attempts (
    id BIGSERIAL PRIMARY KEY,
    ip TEXT,
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX oauth_registration_attempts_ts_idx
    ON oauth_registration_attempts (ts DESC);

CREATE INDEX oauth_registration_attempts_ip_ts_idx
    ON oauth_registration_attempts (ip, ts DESC)
    WHERE ip IS NOT NULL;
