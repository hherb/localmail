-- #426: attachment file names were not searchable. `fts_v2` (0006) indexes
-- subject, from, body and to; the names in `messages.attachments` reached no
-- lexical index, so a search for an exact file name ranked its message by
-- body words alone (25th, in the report).
--
-- An expression index, not a change to the generated `fts_v2` column: that
-- would rewrite every row of `messages`, raw bytes included. The expression
-- is `localmail.search.attachment_names.names_tsvector_sql('attachments')`,
-- character for character, or the planner cannot match it (pinned by
-- tests/test_attachment_names.py). Nothing needs backfilling.
--
-- The name list is cut at 50000 characters (`NAMES_MAX_CHARS`): past the
-- 1 MiB tsvector cap `to_tsvector` raises, and this expression runs on every
-- INSERT into `messages`.
--
-- Lock cost: one CREATE INDEX over `messages`, reading only the small
-- `attachments` column. Blocks writes for its duration; not measured.
CREATE INDEX IF NOT EXISTS messages_attachment_names_idx ON messages
    USING gin ((setweight(to_tsvector('simple', left(jsonb_path_query_array(attachments, '$[*].filename')::text, 50000)) || to_tsvector('simple', regexp_replace(left(jsonb_path_query_array(attachments, '$[*].filename')::text, 50000), '[^[:alnum:]]+', ' ', 'g')), 'A')));
