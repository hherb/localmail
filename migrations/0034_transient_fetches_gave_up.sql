-- Tombstone a UID whose BODY[] sync permanently gave up on (#239).
--
-- #238 bounded the hold on an unfetchable BODY[] so a corrupt or zero-length
-- message could no longer pin a mailbox forever. What it left behind: giving up
-- advanced the watermark and logged one WARNING, and `reclaim_below` then
-- collected the row at the next checkpoint. The message was permanently absent
-- from the archive, invisible to every report, and unreachable by every retry
-- command — the only failure path in this codebase without a queryable,
-- re-drivable row (contrast failed_messages / retry-failed and
-- failed_extractions / retry-failed-extractions).
--
-- `gave_up_at` turns the existing hold row into that record. Sync stamps it at
-- the give-up point; `reclaim_below` now skips stamped rows, so the watermark
-- passing the UID no longer erases the evidence.
--
-- Retention is deliberately manual (`localmail retry-failed-fetches --forget`,
-- optionally `--older-than-days`) rather than an automatic sweep. A tombstone
-- is written once per distinct permanently-unfetchable UID and upserted
-- thereafter, so growth is bounded by the count of genuinely broken messages —
-- not a runaway. Auto-expiry would trade that negligible growth for silently
-- deleting the sole record of permanently lost mail, which is the failure #239
-- exists to end. failed_messages and failed_extractions make the same call.
--
-- The partial index keeps `list-failed-fetches` off the live-hold rows, which
-- are the overwhelmingly common population.

ALTER TABLE transient_fetches ADD COLUMN gave_up_at TIMESTAMPTZ;

CREATE INDEX transient_fetches_gave_up_idx
    ON transient_fetches (gave_up_at)
    WHERE gave_up_at IS NOT NULL;
