CREATE TABLE failed_embeddings (
    id              BIGSERIAL    PRIMARY KEY,
    chunk_table     TEXT         NOT NULL CHECK (chunk_table IN ('message_chunks','attachment_chunks')),
    chunk_id        BIGINT       NOT NULL,
    error_class     TEXT         NOT NULL,
    error_message   TEXT         NOT NULL,
    error_traceback TEXT,
    failed_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retry_count     INT          NOT NULL DEFAULT 0,
    last_retry_at   TIMESTAMPTZ,
    UNIQUE (chunk_table, chunk_id)
);
