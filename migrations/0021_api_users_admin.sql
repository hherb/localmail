-- Admin gate for /admin/* and /v1/admin/*. Bootstrap via shell-only CLI
-- (`localmail grant-admin USERNAME`); the column defaults to FALSE so
-- existing api_users keep their per-account-ACL-only privileges.

ALTER TABLE api_users
  ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- Partial index: admins are the rare case. Lookups are
-- `SELECT * FROM api_users WHERE id = ? AND is_admin = TRUE` from the
-- requires-admin dependency on every admin request.
CREATE INDEX api_users_is_admin_idx ON api_users (id) WHERE is_admin;
