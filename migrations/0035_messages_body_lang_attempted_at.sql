-- #251: unlabelable bodies at the head of the lang-detect queue starved every
-- message behind them.
--
-- `run_lang_detect_pass` (src/localmail/search/lang_detect.py) claims
-- `body_lang IS NULL AND body_text IS NOT NULL ORDER BY id LIMIT N`. A row the
-- detector declines -- below body_lang_min_text_chars, below
-- body_lang_min_confidence, or a body that makes the detector raise -- stays
-- NULL. It therefore still satisfies the predicate and, under a stable
-- ordering, is re-claimed in the same position on every sweep. Once the first
-- body_lang_detect_batch_size rows were all unlabelable the head of the queue
-- was permanently occupied and detection stopped archive-wide: the live Mac
-- archive sat at 7744 labelled against 100020 pending, frozen for weeks.
--
-- `body_lang_attempted_at` records "the detector has run on this body".
-- `body_lang` keeps its exact meaning -- detected language, else unknown -- so
-- no reader changes. That is deliberately unlike the `type-skipped` sentinel
-- #216 used for blobs, which CLAUDE.md documents as a one-way door: lowering a
-- threshold does not re-open the rows it was lowered for.
--
-- Re-open declined rows after loosening detector policy with
--   localmail lang-backfill --retry-declined
-- or by hand:
--   UPDATE messages SET body_lang_attempted_at = NULL WHERE body_lang IS NULL;
--
-- Rows labelled before this migration keep a NULL body_lang_attempted_at. That
-- state is legal and never consulted -- the claim excludes body_lang IS NOT
-- NULL first -- so no backfill of already-labelled rows is needed.
--
-- Lock cost: ADD COLUMN nullable-with-no-default is metadata-only in Postgres
-- 11+. The index build below takes a write lock for a few seconds on a 127k-row
-- archive. Not lock-heavy enough to warrant an `estimate-upgrade` estimator.

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS body_lang_attempted_at TIMESTAMPTZ;

-- Migration 0017's messages_body_lang_pending_idx carries the OLD claim
-- predicate verbatim. It is replaced under a NEW name rather than recreated:
-- `CREATE INDEX IF NOT EXISTS` matches on name only, so reusing the old name
-- with a new predicate would silently no-op on every host that already has it,
-- leaving the worker on an index that no longer matches its claim.
DROP INDEX IF EXISTS messages_body_lang_pending_idx;

CREATE INDEX IF NOT EXISTS messages_body_lang_claimable_idx
    ON messages (id)
    WHERE body_lang IS NULL
      AND body_text IS NOT NULL
      AND body_lang_attempted_at IS NULL;
