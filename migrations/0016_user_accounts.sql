-- Per-user account ACL. Until this lands, every authenticated GUI/API user
-- could read every account's mail. After this lands, each user must be
-- granted access to each account explicitly via `localmail grant-account`.
-- `granted_at` is informational (audit). Both FKs cascade so user removal or
-- account removal cleans the grants automatically.

CREATE TABLE user_accounts (
    user_id     BIGINT      NOT NULL REFERENCES api_users(id) ON DELETE CASCADE,
    account_id  BIGINT      NOT NULL REFERENCES accounts(id)  ON DELETE CASCADE,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, account_id)
);

CREATE INDEX user_accounts_account_id_idx ON user_accounts (account_id);
