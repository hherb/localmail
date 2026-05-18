-- Failed-extractions log (Phase 2). One row per blob (not per
-- (blob, extractor) pair). On retry the row is upserted and
-- retry_count is bumped. The extractor column records the most
-- recent failing extractor — sufficient for diagnostics.

CREATE TABLE failed_extractions (
    sha256          BYTEA       PRIMARY KEY
                                REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    extractor       TEXT        NOT NULL,
    error_class     TEXT        NOT NULL,
    error_message   TEXT        NOT NULL,
    traceback       TEXT,
    retry_count     INT         NOT NULL DEFAULT 0,
    failed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_retry_at   TIMESTAMPTZ
);
