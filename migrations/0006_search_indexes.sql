-- @non-transactional
-- Multilingual tsvector FTS on messages (fts_v2, weighted) and message_chunks (fts),
-- plus HNSW vector index on message_chunks.embedding_v1.
-- Drops the old english-only messages_fts_idx from 0001_init.sql.
-- The @non-transactional header lets the runner use autocommit so that
-- CREATE INDEX CONCURRENTLY is permitted.

-- Generated columns require an IMMUTABLE expression. array_to_string is STABLE,
-- so we wrap it in an IMMUTABLE SQL function to allow use in the generated column.
CREATE OR REPLACE FUNCTION localmail_arr_to_text(TEXT[]) RETURNS TEXT
    LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
    AS $$ SELECT array_to_string($1, ' ') $$;

DROP INDEX IF EXISTS messages_fts_idx;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS fts_v2 tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(subject, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(from_addr, '') || ' ' || coalesce(from_name, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(body_text, '') || ' ' || coalesce(body_html, '')), 'C') ||
        setweight(to_tsvector('simple', coalesce(localmail_arr_to_text(to_addrs), '')), 'D')
    ) STORED;

CREATE INDEX IF NOT EXISTS messages_fts_v2_idx ON messages USING GIN (fts_v2);

ALTER TABLE message_chunks
    ADD COLUMN IF NOT EXISTS fts tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', text)
    ) STORED;

CREATE INDEX IF NOT EXISTS message_chunks_fts_idx ON message_chunks USING GIN (fts);

CREATE INDEX CONCURRENTLY IF NOT EXISTS message_chunks_embedding_v1_hnsw
    ON message_chunks USING hnsw (embedding_v1 halfvec_cosine_ops)
    WITH (m=16, ef_construction=64);
