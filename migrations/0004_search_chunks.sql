CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE message_chunks (
    id              BIGSERIAL    PRIMARY KEY,
    message_id      BIGINT       NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind            TEXT         NOT NULL CHECK (kind IN ('header', 'body')),
    chunk_idx       INT          NOT NULL,
    text            TEXT         NOT NULL,
    token_count     INT          NOT NULL,
    embedding_v1    halfvec(768),
    embedded_at     TIMESTAMPTZ,
    UNIQUE (message_id, kind, chunk_idx)
);

CREATE INDEX message_chunks_msg_idx ON message_chunks (message_id);
CREATE INDEX message_chunks_pending_idx
    ON message_chunks (id) WHERE embedding_v1 IS NULL;
