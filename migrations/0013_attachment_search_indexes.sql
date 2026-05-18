-- @non-transactional
-- Arm 4 indexes for attachment search.
--
-- CREATE INDEX CONCURRENTLY cannot run inside a transaction, so this migration
-- requires autocommit mode, which the runner provides when it sees @non-transactional.
--
-- Note: _split_statements in db.py splits on every semicolon character. This migration
-- deliberately contains no semicolons inside string literals or dollar-quoted blocks,
-- so the naive split is safe. If you ever add a dollar-quoted block here, update
-- _split_statements first.
--
-- HNSW parameters (m=16, ef_construction=64) match Phase 1's
-- message_chunks_embedding_v1_hnsw from 0006_search_indexes.sql for
-- consistent build cost and recall characteristics across both chunk tables.

CREATE INDEX CONCURRENTLY IF NOT EXISTS attachment_chunks_embedding_v1_hnsw
    ON attachment_chunks USING hnsw (embedding_v1 halfvec_cosine_ops)
    WITH (m=16, ef_construction=64);

CREATE INDEX CONCURRENTLY IF NOT EXISTS messages_attachments_gin
    ON messages USING GIN (attachments);
