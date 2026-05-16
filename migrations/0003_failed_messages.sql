-- Persistent record of every message that sync skipped due to an exception
-- (DB rejection, parser failure, encoding crash, etc.). The raw RFC822 bytes
-- are stored so the message can be re-attempted later with an improved parser
-- *without* re-fetching from IMAP (which may have deleted the message in the
-- meantime, or — for archived folders — may not even be reachable any more).

CREATE TABLE failed_messages (
    id              BIGSERIAL    PRIMARY KEY,
    account_id      BIGINT       NOT NULL REFERENCES accounts(id)  ON DELETE CASCADE,
    mailbox_id      BIGINT       NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    uid             BIGINT       NOT NULL,
    raw_bytes       BYTEA        NOT NULL,
    raw_sha256      BYTEA        NOT NULL,
    error_class     TEXT         NOT NULL,
    error_message   TEXT         NOT NULL,
    error_traceback TEXT,
    failed_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retry_count     INT          NOT NULL DEFAULT 0,
    last_retry_at   TIMESTAMPTZ,
    UNIQUE (account_id, mailbox_id, uid)
);
