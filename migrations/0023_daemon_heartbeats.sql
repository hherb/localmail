-- Daemon liveness heartbeats (2B.2, Plane A — DB-mediated, supervisor-agnostic).
-- One row per account thread (worker_kind in idle/poll, account_id NOT NULL) and
-- one per process-level worker (embed/extract/reconcile, account_id NULL). Each
-- worker upserts its row at the top of every loop iteration and on state
-- transitions; the admin daemon-status reader derives liveness purely from
-- now() - last_heartbeat_at. Multi-host clustering is a non-goal, so the daemon
-- DELETEs every row once at startup (see localmail.heartbeat.clear_all_heartbeats)
-- — leftover rows from a crashed previous run never read as live.

CREATE TABLE daemon_heartbeats (
    id                BIGSERIAL    PRIMARY KEY,
    worker_kind       TEXT         NOT NULL
                                   CHECK (worker_kind IN
                                          ('idle','poll','embed','extract','reconcile')),
    account_id        BIGINT       REFERENCES accounts(id) ON DELETE CASCADE,
    state             TEXT         NOT NULL
                                   CHECK (state IN
                                          ('starting','connecting','idle','polling',
                                           'syncing','error','reconnecting','stopped')),
    current_folder    TEXT,
    last_error_msg    TEXT,
    started_at        TIMESTAMPTZ  NOT NULL,
    last_heartbeat_at TIMESTAMPTZ  NOT NULL
);

-- Two partial unique indexes rather than one: each is an independent, named
-- ON CONFLICT target — one for account threads (account_id NOT NULL), one for
-- process-level workers (account_id IS NULL).
CREATE UNIQUE INDEX daemon_heartbeats_acct_idx
    ON daemon_heartbeats (worker_kind, account_id) WHERE account_id IS NOT NULL;

CREATE UNIQUE INDEX daemon_heartbeats_proc_idx
    ON daemon_heartbeats (worker_kind) WHERE account_id IS NULL;
