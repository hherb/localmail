-- Server-side polling cursors for named subscriptions (one per api-user +
-- name). Lets a polling client be stateless: poll, process, ack. Without this
-- a cursorless client re-reads the 200 most recent messages on every restart.
CREATE TABLE channel_subscriptions (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     BIGINT      NOT NULL REFERENCES api_users(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    cursor      BIGINT      NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);
