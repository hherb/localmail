-- Transient-extraction counter (#153). Caps the docling third-party
-- network-error retry loop introduced by #47 WITHOUT touching the
-- poison-pill semantics of `failed_extractions.retry_count`.
--
-- Background: #47 routes docling's third-party network failures
-- (huggingface_hub / requests / httpx / urllib3 / aiohttp) through the
-- *transient* path — ROLLBACK + WARNING, no `failed_extractions` row, so a
-- genuine model-download blip retries next sweep with retry_count untouched.
-- The gap: a *permanently* failing third-party error (HF 401/403 from a bad
-- token, 404 for a removed model) re-attempts every sweep forever.
--
-- This table holds a transient-failure counter that is deliberately
-- INDEPENDENT of `failed_extractions.retry_count` (which stays reserved for
-- true poison-pills). The extract worker bumps `transient_count` on each
-- transient classification, the claim query excludes a blob once the count
-- reaches `extract_worker_max_transient_retries`, and a successful extraction
-- clears the row (so the cap counts *consecutive* transient failures).
-- One row per blob, upserted on each transient failure (mirrors
-- `failed_extractions`).

CREATE TABLE transient_extractions (
    sha256             BYTEA       PRIMARY KEY
                                   REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    transient_count    INT         NOT NULL DEFAULT 0,
    error_class        TEXT,
    error_message      TEXT,
    first_transient_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_transient_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
