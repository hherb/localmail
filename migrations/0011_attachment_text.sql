-- Attachment text + chunks tables (Phase 2).
-- Per-blob extracted text and chunk rows keyed on the blob's sha256,
-- not on message_id — the content-addressable blob design means one
-- chunk set per unique byte sequence regardless of how many messages
-- reference it.

CREATE TABLE attachment_text (
    sha256          BYTEA       PRIMARY KEY
                                REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    extractor       TEXT        NOT NULL,
    extracted_text  TEXT        NOT NULL,
    page_count      INT,
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE attachment_chunks (
    id              BIGSERIAL    PRIMARY KEY,
    sha256          BYTEA        NOT NULL
                                 REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    chunk_idx       INT          NOT NULL,
    text            TEXT         NOT NULL,
    token_count     INT          NOT NULL,
    embedding_v1    halfvec(768),
    embedded_at     TIMESTAMPTZ,
    UNIQUE (sha256, chunk_idx)
);

CREATE INDEX attachment_chunks_blob_idx
    ON attachment_chunks (sha256);
CREATE INDEX attachment_chunks_pending_idx
    ON attachment_chunks (id) WHERE embedding_v1 IS NULL;
