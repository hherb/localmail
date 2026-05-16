CREATE TABLE failed_chunkings (
    message_id      BIGINT       PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    error_class     TEXT         NOT NULL,
    error_message   TEXT         NOT NULL,
    error_traceback TEXT,
    failed_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retry_count     INT          NOT NULL DEFAULT 0,
    last_retry_at   TIMESTAMPTZ
);
