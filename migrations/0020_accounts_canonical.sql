-- 0020_accounts_canonical.sql
-- Promote the accounts table to be authoritative (DB-canonical), as
-- planned by the admin UI design doc (2026-05-28).
--
-- This migration is intentionally idempotent (every ALTER uses
-- IF (NOT) EXISTS-style guards) so re-running on a partially-migrated
-- archive is safe.

BEGIN;

-- Folder-filter columns (currently held in config.toml).
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS folder_allow      JSONB;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS folder_deny       JSONB;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS folder_deny_flags JSONB;

-- v1.x reservation: per-account sync pause. Daemon does NOT honor it yet.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS sync_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- Audit timestamp for the admin UI.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Allow the 'archive' auth method (mbox import lands in v1's Sub-plan 2C).
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_auth_method_check;
ALTER TABLE accounts ADD  CONSTRAINT accounts_auth_method_check
  CHECK (auth_method IN ('password', 'oauth2', 'archive'));

-- Live IMAP accounts must have host + port; archive accounts must not.
-- Lift the legacy NOT NULL on imap_host / imap_port first so 'archive'
-- accounts can NULL them.
ALTER TABLE accounts ALTER COLUMN imap_host DROP NOT NULL;
-- The DEFAULT 993 on imap_port is deliberately preserved — removing it
-- would break existing test/CLI callers that omit the column on
-- password/oauth2 inserts. Archive callers must pass imap_port = NULL
-- explicitly (the bidirectional CHECK below enforces correctness).
ALTER TABLE accounts ALTER COLUMN imap_port DROP NOT NULL;

ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_live_requires_host;
ALTER TABLE accounts ADD  CONSTRAINT accounts_live_requires_host
  CHECK (
    (auth_method = 'archive'
      AND imap_host IS NULL AND imap_port IS NULL)
    OR
    (auth_method IN ('password', 'oauth2')
      AND imap_host IS NOT NULL AND imap_port IS NOT NULL)
  );

COMMIT;
