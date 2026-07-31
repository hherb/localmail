-- Bounded retry bookkeeping for an empty BODY[] on FETCH (#222A).
--
-- Background: a UID present in the batch whose FETCH returns no BODY[] used to
-- be folded into the `mailboxes.uidnext` watermark with only a WARNING, so a
-- transient server hiccup lost the message permanently. Sync now probes
-- (SEARCH UID n:n) to tell an expunged message from a still-present one and
-- holds the resume point on the latter so the next run re-fetches it.
--
-- The gap that hold alone leaves: "still present" is not "will ever be
-- fetchable". A zero-length message reads as no-body yet is genuinely there,
-- and a corrupt store entry can omit the body forever — either would pin the
-- mailbox permanently, re-fetching the whole tail on every run (and the IDLE
-- thread re-syncs INBOX on *every* notification, so that is per new mail).
--
-- This table bounds it by *time*: sync gives up and advances once
-- `first_seen_at` is older than `[daemon] max_body_fetch_hold_s`, and clears
-- the row on a successful fetch — so the window measures one *continuous*
-- outage. Deliberately a duration, not an attempt count à la
-- `transient_extractions` (#153): that counter is driven by a timer-paced
-- sweep, whereas sync passes are event-driven (one per IDLE notification), so
-- a count here would be spent at the mailbox's traffic rate.

CREATE TABLE transient_fetches (
    mailbox_id    BIGINT      NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    uid           BIGINT      NOT NULL,
    attempt_count INT         NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (mailbox_id, uid)
);
