-- Append-only log of every /v1/auth/login attempt.
-- The three sliding-window caps (global, per-IP, per-user) read indexed
-- COUNTs over this table, so the limits survive uvicorn --workers N and
-- localmail serve restarts. Rows older than auth.login_attempt_retention_s
-- are best-effort deleted by the in-process sweep gated on a PG advisory
-- lock; see localmail.api.auth._sweep_login_attempts.

CREATE TABLE api_login_attempts (
    id          BIGSERIAL    PRIMARY KEY,
    ts          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    ip          TEXT,
    username    TEXT         NOT NULL,
    outcome     TEXT         NOT NULL
                             CHECK (outcome IN ('success','failure'))
);

CREATE INDEX api_login_attempts_ts_idx
    ON api_login_attempts (ts DESC);

CREATE INDEX api_login_attempts_ip_ts_idx
    ON api_login_attempts (ip, ts DESC)
    WHERE ip IS NOT NULL;

CREATE INDEX api_login_attempts_user_ts_idx
    ON api_login_attempts (username, ts DESC);
