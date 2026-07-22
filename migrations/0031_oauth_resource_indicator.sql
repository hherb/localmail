-- RFC 8707 resource indicators. The audience a token is bound to is carried
-- from /authorize onto the authorization code, then onto the minted access +
-- refresh tokens, and enforced at /mcp (load_access). NULL = unrestricted, so
-- /v1/auth/login tokens and pre-migration rows are structurally immune.

ALTER TABLE oauth_authorization_codes ADD COLUMN resource TEXT;
ALTER TABLE oauth_refresh_tokens      ADD COLUMN resource TEXT;
ALTER TABLE api_tokens                ADD COLUMN oauth_resource TEXT;
