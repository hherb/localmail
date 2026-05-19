-- Partial index for the lang-detect worker's claim query.
-- `run_lang_detect_pass` (src/localmail/search/lang_detect.py) selects rows
-- matching `body_lang IS NULL AND body_text IS NOT NULL ORDER BY id LIMIT N
-- FOR UPDATE SKIP LOCKED`. Migration 0015 indexed the inverse predicate
-- (`body_lang IS NOT NULL`) for `lang:` filter probes; that index cannot
-- service the worker, which falls back to a seq scan that gets slower as
-- the labelled fraction approaches 100%.
--
-- The predicate matches the worker query verbatim so the planner picks
-- this index instead of seq-scanning. Symmetric to
-- `messages_body_lang_idx` so the two indexes together cover both
-- read paths (filter + claim) without overlapping.

CREATE INDEX IF NOT EXISTS messages_body_lang_pending_idx
    ON messages (id)
    WHERE body_lang IS NULL AND body_text IS NOT NULL;
