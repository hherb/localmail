-- Archive-import job tracking (Sub-plan 2A.5, /admin/imports).
-- One row per import run: an mbox file or maildir directory streamed into an
-- archive account by an in-serve worker thread. Per-message poison pills still
-- land in failed_messages; `failed` here is only the running display count.

CREATE TABLE import_jobs (
    id               BIGSERIAL    PRIMARY KEY,
    account_id       BIGINT       NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_kind      TEXT         NOT NULL CHECK (source_kind IN ('mbox','maildir')),
    source_path      TEXT         NOT NULL,
    status           TEXT         NOT NULL CHECK (status IN
                        ('pending','running','completed','failed','cancelled')),
    total_messages   BIGINT,
    processed        BIGINT       NOT NULL DEFAULT 0,
    inserted         BIGINT       NOT NULL DEFAULT 0,
    skipped_dup      BIGINT       NOT NULL DEFAULT 0,
    failed           BIGINT       NOT NULL DEFAULT 0,
    error_msg        TEXT,
    cancel_requested BOOLEAN      NOT NULL DEFAULT FALSE,
    last_progress_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);

-- Busy-guard: at most ONE active import at a time. Unique on a constant
-- expression over the active subset, so any second pending/running row
-- violates it (a unique index on (status) would wrongly permit one pending
-- AND one running simultaneously).
CREATE UNIQUE INDEX import_jobs_single_active_uniq
    ON import_jobs ((TRUE))
    WHERE status IN ('pending','running');

-- List newest-first, scoped by account.
CREATE INDEX import_jobs_account_idx ON import_jobs (account_id, id DESC);
