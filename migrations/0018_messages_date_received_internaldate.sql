-- Add `messages.internal_date` to hold the RFC 3501 INTERNALDATE — the
-- timestamp the IMAP server recorded when the message arrived in its
-- mailbox. This is the value users mean by "received date" when sorting
-- mail: it survives across syncs and reflects when the email actually
-- arrived, unlike the existing `date_received` column which was
-- `DEFAULT now()` and so reflects sync time.
--
-- `date_received` is kept as-is — it's the local "when did we write
-- this row" timestamp, which has audit value and is referenced by
-- `/v1/changes` as a safe-horizon filter. Renaming or wiping it would
-- break the polling endpoint and forfeit the sync-log provenance.
--
-- Population strategy:
--   * `sync.py:upsert_message` will pass INTERNALDATE through to this
--     column on insert (new mail going forward).
--   * `localmail backfill-internal-date` re-runs FETCH UID INTERNALDATE
--     for every existing UID and UPDATEs the column. Body bytes are not
--     refetched so the pass is fast.
--
-- Ordering will use `COALESCE(internal_date, date_sent) DESC` so rows
-- backfilled to a real INTERNALDATE sort by that, and un-backfilled
-- rows fall through to the email header `Date:` — exactly the
-- "INTERNALDATE if available, else date_sent" intent.

ALTER TABLE messages ADD COLUMN internal_date TIMESTAMPTZ;

-- Helps `ORDER BY COALESCE(internal_date, date_sent) DESC LIMIT N` on
-- the recent-mail / empty-query path. Postgres can use this expression
-- index to avoid sorting the entire table on every request.
CREATE INDEX messages_recent_idx
    ON messages ((COALESCE(internal_date, date_sent)) DESC NULLS LAST, id DESC);
