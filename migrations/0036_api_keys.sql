-- Admin-issued API keys: a named, never-expiring credential for a machine
-- consumer, minted against a dedicated service user.
--
-- `api_key_name IS NOT NULL` IS the credential kind. There is deliberately no
-- second boolean beside it that could disagree. The column is `api_key_name`
-- rather than `name` so that a future "let users label their sessions" feature
-- must add its own column instead of inheriting API-key semantics -- an
-- immortal credential barred from admin routes -- by writing to a field that
-- merely sounds general.
--
-- Dropping NOT NULL from expires_at on its own would let a *login* token be
-- minted with no expiry: an immortal interactive credential, produced by a
-- one-line bug, with nothing failing and no query that would look wrong. The
-- CHECK scopes "may live forever" to API keys, here, where no code path routes
-- around it.
--
-- The unique index is keyed on user_id alone, not (user_id, api_key_name): the
-- pair would permit several differently-named keys on one principal, which is
-- the many-keys model the design defers. Key names are unique globally for
-- free, via the existing api_users.username unique constraint.
--
-- Lock cost: all three ALTERs are metadata-only in Postgres 11+ (ADD COLUMN
-- nullable, ADD COLUMN with a constant default, DROP NOT NULL). The CHECK is
-- validated against existing rows, and the index build takes a brief write
-- lock; api_tokens holds one row per live session, so both are trivial.

ALTER TABLE api_tokens
    ADD COLUMN IF NOT EXISTS api_key_name TEXT;

ALTER TABLE api_tokens
    ALTER COLUMN expires_at DROP NOT NULL;

ALTER TABLE api_tokens DROP CONSTRAINT IF EXISTS api_tokens_only_keys_are_immortal;
ALTER TABLE api_tokens ADD  CONSTRAINT api_tokens_only_keys_are_immortal
    CHECK (api_key_name IS NOT NULL OR expires_at IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS api_tokens_one_key_per_service_user
    ON api_tokens (user_id)
    WHERE api_key_name IS NOT NULL;

ALTER TABLE api_users
    ADD COLUMN IF NOT EXISTS is_service BOOLEAN NOT NULL DEFAULT FALSE;
