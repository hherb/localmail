-- #429: 0037's index read attachment names with a lax jsonpath, which wraps a
-- top-level object and unwraps a nested array, and an array-valued name still
-- reached both rules, so those malformed `attachments` shapes matched a name
-- search while HAS_ATTACHMENT_SQL read the object as "no attachments". The path is strict
-- now (`localmail.search.attachment_names.NAMES_JSONPATH`), for the lexical
-- match and the `attachment_name:` filter alike.
--
-- A new index name, not 0037's: `CREATE INDEX IF NOT EXISTS` matches on the
-- name only, so reusing it would silently keep the lax expression on every
-- host that already has 0037 (0035's lesson). The expression is
-- `names_tsvector_sql('attachments')`, character for character, or the
-- planner cannot match it (pinned by tests/test_attachment_names.py).
--
-- Lock cost: one CREATE INDEX over `messages`, reading only the small
-- `attachments` column. Computing the expression over the 132k-message Mac
-- archive took 0.2 s; the GIN build adds to that. Blocks writes meanwhile.
DROP INDEX IF EXISTS messages_attachment_names_idx;
CREATE INDEX IF NOT EXISTS messages_attachment_names_strict_idx ON messages
    USING gin ((setweight(to_tsvector('simple', left(jsonb_path_query_array(attachments, 'strict $[*] ? (@.type() == "object" && @.filename.type() == "string").filename', '{}', true)::text, 50000)) || to_tsvector('simple', regexp_replace(left(jsonb_path_query_array(attachments, 'strict $[*] ? (@.type() == "object" && @.filename.type() == "string").filename', '{}', true)::text, 50000), '[^[:alnum:]]+', ' ', 'g')), 'A')));
