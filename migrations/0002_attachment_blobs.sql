-- Content-addressable attachment storage.
-- One row per unique byte sequence; multiple messages can reference the same
-- blob (a forwarded PDF, a newsletter image, etc.) without duplicating bytes
-- on disk. The `messages.attachments` JSONB column now stores entries of the
-- shape `{"filename": "...", "sha256": "<64-char hex>"}` per attachment; the
-- mime type and size live on the blob row.

CREATE TABLE attachment_blobs (
    sha256        BYTEA       PRIMARY KEY,
    path          TEXT        NOT NULL,
    mime_type     TEXT,
    size_bytes    BIGINT      NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
