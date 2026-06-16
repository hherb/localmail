-- Access-token family containment on refresh-token reuse. OAuth-minted access
-- tokens are tagged with the refresh family they belong to so that, on
-- refresh-token reuse detection (oauth_refresh_tokens family DELETE), the
-- access tokens in the same family can be purged immediately rather than
-- lingering until their <=1h TTL (closes the #186 accepted limitation).
-- NULL on /v1/auth/login tokens and pre-migration rows -- structurally immune
-- to the family purge. UUID matches oauth_refresh_tokens.family_id (0029).

ALTER TABLE api_tokens
    ADD COLUMN oauth_refresh_family_id UUID;

CREATE INDEX api_tokens_oauth_refresh_family_id_idx
    ON api_tokens (oauth_refresh_family_id)
    WHERE oauth_refresh_family_id IS NOT NULL;
