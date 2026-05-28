-- Server-side session revocation knob (issue #113).
--
-- Admin cookie sessions are stateless HMAC-signed tokens. Without a
-- revocation hook, a leaked cookie stays valid until its 8 h `exp` and the
-- only way to evict it is to rotate `session_signing_key` (which kicks out
-- every admin including the operator running the rotation).
--
-- `sessions_invalidated_at` records "this user's earliest still-valid token
-- issued_at". `require_admin_session` rejects tokens with
-- `to_timestamp(payload.issued_at) < sessions_invalidated_at`, so bumping
-- the column to now() forces every outstanding session to re-login on the
-- next request. NULL is the default and means "no revocation ever issued"
-- — every valid token is accepted as today.
--
-- Bumped via `localmail revoke-admin-sessions USERNAME` (shell-only;
-- mirrors the grant-admin / revoke-admin bootstrap path).

ALTER TABLE api_users
  ADD COLUMN sessions_invalidated_at TIMESTAMPTZ;
