-- Daemon command queue (2B.3, Plane A — DB-mediated, supervisor-agnostic).
-- Carries imperative actions that are NOT expressible as desired account state:
--   reload-now       force an immediate reconcile (don't wait out reload_seconds)
--   restart-account  teardown + respawn one account's thread bundle (account_id required)
--   drain-stop       set the master stop event; the daemon drains and exits
-- Add/remove/pause/resume an account stay as `accounts` edits the reconcile picks
-- up — they are NOT commands (see the 2B re-spec, decision 2). The daemon drains
-- this queue at the top of each reconcile tick (FOR UPDATE SKIP LOCKED) and an
-- enqueue NOTIFYs the `daemon_commands` channel so a listening daemon wakes early.
-- Single-instance daemon is assumed (multi-host is a non-goal); SKIP LOCKED is
-- defensive, not a clustering claim.

CREATE TABLE daemon_commands (
    id           BIGSERIAL    PRIMARY KEY,
    command      TEXT         NOT NULL
                              CHECK (command IN
                                     ('reload-now','restart-account','drain-stop')),
    -- required iff restart-account; forbidden otherwise (enforced by the CHECK below)
    account_id   BIGINT       REFERENCES accounts(id) ON DELETE CASCADE,
    state        TEXT         NOT NULL DEFAULT 'queued'
                              CHECK (state IN ('queued','done','failed')),
    requested_by INT          REFERENCES api_users(id),
    requested_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    picked_at    TIMESTAMPTZ,
    done_at      TIMESTAMPTZ,
    result_msg   TEXT,
    CHECK ((command = 'restart-account') = (account_id IS NOT NULL))
);

-- Partial index over only the queued rows the consumer scans, oldest first.
CREATE INDEX daemon_commands_queue_idx
    ON daemon_commands (requested_at) WHERE state = 'queued';
