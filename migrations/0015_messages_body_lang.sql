-- body_lang on messages: ISO 639-1 / -2 code populated by the embed worker
-- per-message language detection. Nullable so existing rows aren't affected;
-- the `lang:` search DSL token filters on this column when present.
-- Empty/unknown detections must remain NULL — clients that opt into lang
-- filtering accept that NULL rows are excluded.

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS body_lang TEXT;

CREATE INDEX IF NOT EXISTS messages_body_lang_idx
    ON messages (body_lang)
    WHERE body_lang IS NOT NULL;
