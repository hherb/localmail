-- localmail schema, revision 0001.

CREATE TABLE IF NOT EXISTS schema_migrations (
    revision     TEXT        PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
    id              BIGSERIAL    PRIMARY KEY,
    name            TEXT         NOT NULL UNIQUE,
    email_address   TEXT         NOT NULL,
    imap_host       TEXT         NOT NULL,
    imap_port       INT          NOT NULL DEFAULT 993,
    auth_method     TEXT         NOT NULL CHECK (auth_method IN ('password','oauth2')),
    oauth_provider  TEXT,
    config          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE mailboxes (
    id            BIGSERIAL    PRIMARY KEY,
    account_id    BIGINT       NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name          TEXT         NOT NULL,
    delimiter     TEXT,
    flags         TEXT[],
    uidvalidity   BIGINT,
    uidnext       BIGINT,
    last_sync_at  TIMESTAMPTZ,
    UNIQUE (account_id, name)
);

CREATE TABLE messages (
    id             BIGSERIAL    PRIMARY KEY,
    account_id     BIGINT       NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    message_id     TEXT,
    raw_sha256     BYTEA        NOT NULL,
    in_reply_to    TEXT,
    refs           TEXT[],
    subject        TEXT,
    from_addr      TEXT,
    from_name      TEXT,
    to_addrs       TEXT[],
    cc_addrs       TEXT[],
    bcc_addrs      TEXT[],
    date_sent      TIMESTAMPTZ,
    date_received  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    headers        JSONB        NOT NULL,
    body_text      TEXT,
    body_html      TEXT,
    raw_bytes      BYTEA        NOT NULL,
    size_bytes     INT          NOT NULL,
    attachments    JSONB        NOT NULL DEFAULT '[]'::jsonb
);

CREATE UNIQUE INDEX messages_acct_msgid_uniq
    ON messages (account_id, message_id) WHERE message_id IS NOT NULL;

CREATE UNIQUE INDEX messages_acct_rawsha_uniq
    ON messages (account_id, raw_sha256) WHERE message_id IS NULL;

CREATE INDEX messages_acct_date_idx
    ON messages (account_id, date_sent DESC);

CREATE INDEX messages_headers_gin
    ON messages USING GIN (headers);

CREATE INDEX messages_fts_idx
    ON messages USING GIN (to_tsvector('english', coalesce(body_text, '')));

CREATE TABLE message_labels (
    message_id  BIGINT  NOT NULL REFERENCES messages(id)  ON DELETE CASCADE,
    mailbox_id  BIGINT  NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    uid         BIGINT  NOT NULL,
    flags       TEXT[],
    PRIMARY KEY (message_id, mailbox_id),
    UNIQUE (mailbox_id, uid)
);
