-- API users and bearer tokens for the GUI HTTP server.
-- Tokens are stored as SHA-256 hashes of the raw bearer string;
-- a DB compromise must not hand out usable tokens.

CREATE TABLE api_users (
    id              BIGSERIAL    PRIMARY KEY,
    username        TEXT         NOT NULL UNIQUE,
    password_hash   TEXT         NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    disabled_at     TIMESTAMPTZ
);

CREATE TABLE api_tokens (
    token_sha256    BYTEA        PRIMARY KEY,
    user_id         BIGINT       NOT NULL REFERENCES api_users(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ  NOT NULL,
    last_used_at    TIMESTAMPTZ
);

CREATE INDEX api_tokens_user_id_idx   ON api_tokens (user_id);
CREATE INDEX api_tokens_expires_at_idx ON api_tokens (expires_at);
