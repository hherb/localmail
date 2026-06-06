-- Ownership metadata for import_jobs (#162): record the host + pid of the
-- process that created (and runs) each import. Serve-startup reconcile then
-- reaps only genuinely orphaned jobs (a dead pid on this host, or a NULL
-- owner), leaving a live CLI import's row -- and the single-active busy-guard
-- -- intact. Nullable: any pre-existing active row has a NULL owner and is
-- treated as orphaned by reconcile.

ALTER TABLE import_jobs ADD COLUMN owner_host TEXT;
ALTER TABLE import_jobs ADD COLUMN owner_pid  INTEGER;
