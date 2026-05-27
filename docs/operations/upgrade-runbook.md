# Large-archive upgrade runbook

When you run `localmail init-db` for the first time against a populated
`messages` table — say you imported from another archive, or you're
adopting localmail against an existing Postgres database — some
migrations hold long locks that block all writes for the duration of
the build. This runbook tells you what to expect, how to estimate the
cost ahead of time, and how to mitigate.

Fresh installs (empty tables) are unaffected. The migrations all
complete in seconds.

## When to read this

Read this before running `localmail init-db` if **any** of these apply:

- You imported an existing IMAP archive into a Postgres `messages`
  table outside of localmail.
- You're restoring from a `pg_dump` taken before a localmail release
  that introduced new lock-heavy migrations.
- You're running localmail in production with synchronous writers
  that must not stall (e.g. an indexer that polls the daemon).
- Your archive has more than a few hundred thousand `messages` rows.

If none of these apply, just run `localmail init-db`. It's fine.

## The lock-heavy migrations

| Revision | Holds lock for | What it does |
|---|---|---|
| `0006_search_indexes` | Minutes to hours, depending on row count | Adds `messages.fts_v2` (`tsvector` stored generated column) and two GIN indexes. Two sources of lock: (1) `ADD COLUMN ... GENERATED ALWAYS AS ... STORED` rewrites the whole heap under `ACCESS EXCLUSIVE`; (2) `CREATE INDEX ... USING GIN` (no `CONCURRENTLY`) holds `ShareLock` for the build. |
| `0015_messages_body_lang` | Seconds to a minute | Adds `messages.body_lang` (nullable, no default — metadata-only, no rewrite) plus a partial btree index. Small. |
| `0018_messages_date_received_internaldate` | Seconds to minutes | Adds `messages.internal_date` (nullable, no default — no rewrite) plus the `messages_recent_idx` btree expression index. Build time scales with row count but is much cheaper than the GIN builds. |

Only `0006_search_indexes` is dangerous at scale. Estimators for the
others can be added in a follow-up if any operator reports needing
them.

## Pre-flight: `localmail estimate-upgrade`

Run this against your live archive **before** running `init-db`:

```bash
unset VIRTUAL_ENV && uv run localmail estimate-upgrade
```

Example output for a 500k-row archive on SSD:

```
revision: 0006_search_indexes
  status: pending
  fts_v2 (projected):     1,245,000,000 bytes (1187.5 MiB)
  gin_messages (projected):  498,000,000 bytes (475.0 MiB)
  gin_chunks (projected):              0 bytes (0.0 MiB)
  projected lock duration: ~6m 12s
  WARNING: message_chunks GIN size cannot be projected before chunks exist;
    rerun after the embed worker has populated chunks for an accurate estimate.
```

How to read each line:

- **`fts_v2 (projected)`** — additional storage the new column will
  consume on disk. Roughly 1.5× the concatenated `subject + body_text
  + body_html` text length per row.
- **`gin_messages (projected)`** — additional storage for the GIN
  index over `fts_v2`. Typically 40% of the column size.
- **`gin_chunks (projected)`** — additional storage for the GIN
  index over `message_chunks.fts`. On a fresh archive
  `message_chunks` is empty (no embed worker has run yet) so the
  estimator reports `0 bytes` and surfaces a warning that the
  number is a lower bound. On an archive where the embed worker
  has already populated `message_chunks` — which happens when
  0004 is applied but 0006 is still pending, or on a re-estimate
  after a partial run — the projection sums the chunks-GIN with
  the messages-GIN and the warning is suppressed. The populated
  branch issues a single `count(*) + avg(octet_length(text))`
  scan of `message_chunks`; on archives with tens of millions of
  chunks expect the estimator itself to take a few seconds.
- **`projected lock duration`** — sum of the table-rewrite duration
  (driven by `table_rewrite_mb_per_sec` in config) and the
  GIN-build duration (driven by `gin_build_mb_per_sec`). These are
  rough; see "Calibration" below.

JSON output for scripting:

```bash
unset VIRTUAL_ENV && uv run localmail estimate-upgrade --format json
```

Output: a list of objects, one per registered estimator. Stable
schema: `revision`, `status`, `current_bytes`, `projected_bytes`,
`projected_duration_s`, `warnings`. Empty dicts/lists for
not-applicable branches.

## Recommended procedure for 0006 at scale

Pick one of these based on your tolerance for write downtime:

### Option A: schedule downtime (simplest, recommended for most operators)

1. Run `localmail estimate-upgrade` to get a duration estimate.
2. Schedule a maintenance window of `2 × estimated_duration` (the 2×
   absorbs ETA error from the throughput-rate defaults and gives
   you room to investigate if something goes wrong; if the
   estimator output included the chunks-GIN cannot-be-projected
   warning, the projection is a lower bound and the safety margin
   is consumed accordingly).
3. Stop the daemon, any cron jobs, and any external writers.
4. `pg_dump` the database (always — see "Disk-space planning" below
   for the size impact).
5. Run `unset VIRTUAL_ENV && uv run localmail init-db`. Tail the
   Postgres log if you want progress visibility — there's no localmail
   progress bar for migrations.
6. Run `localmail estimate-upgrade` again. The output should now show
   `status: applied` with `current_bytes` populated.
7. Restart the daemon.

### Option B: online column-rename procedure (advanced, requires Postgres ops chops)

You can avoid the `ACCESS EXCLUSIVE` lock by building a shadow
column with the same definition, backfilling it in batches under
short locks, creating the GIN index `CONCURRENTLY`, then renaming.
This is significantly more work and easy to get wrong; **if you
don't immediately know why each step below is needed, schedule
downtime (Option A) instead.**

The high-level shape:

1. Stop the daemon (writes would race the swap).
2. `ALTER TABLE messages ADD COLUMN fts_v2_new tsvector;`
3. Backfill in batches of ~10k rows, each in its own transaction.
4. `CREATE INDEX CONCURRENTLY messages_fts_v2_new_idx ON messages USING GIN (fts_v2_new);`
5. In a single transaction with `lock_timeout = '5s'`:
   - `DROP INDEX messages_fts_v2_idx;` (if it exists — won't on a
     fresh archive)
   - `ALTER TABLE messages DROP COLUMN fts_v2;` (if it exists)
   - `ALTER TABLE messages RENAME COLUMN fts_v2_new TO fts_v2;`
   - `ALTER INDEX messages_fts_v2_new_idx RENAME TO messages_fts_v2_idx;`
   - Add the same trigger/generated-column expression to keep
     `fts_v2` populated on subsequent inserts. (Note: a `STORED`
     generated column can't be added to an existing column without
     a rewrite — so this option ships a regular column populated
     by a `BEFORE INSERT` trigger instead. The migration's `IF NOT
     EXISTS` clauses on `ADD COLUMN` and `CREATE INDEX` will then
     no-op when `init-db` is finally run.)
6. Mark `0006_search_indexes` as applied:
   `INSERT INTO schema_migrations (revision) VALUES ('0006_search_indexes');`
7. Restart the daemon.

This procedure has real trade-offs (extra storage during the swap,
brief `AccessExclusiveLock` on the rename, a trigger-vs-generated
behavioural difference for new rows). Test it on a clone of your
database first. If you can't articulate why each step is here,
Option A is the right call.

## Disk-space planning

Migration 0006 needs **roughly 2× the current `messages` table
size in free disk** during the run:

- The table rewrite produces a new heap before swapping; the old
  heap is reclaimed by autovacuum after the migration commits.
- The GIN indexes also need to be built before they're swapped in.
- `pg_dump` (always recommended pre-migration) adds another copy
  to wherever you write the dump.

Quick check:

```bash
psql -c "SELECT pg_size_pretty(pg_total_relation_size('messages')) AS messages_size;"
df -h $(psql -tA -c "SHOW data_directory;")
```

If the data directory has less than 3× the `messages_size` free,
free up space or move the dump elsewhere before starting.

## Calibration

The defaults assume SSD + modern Postgres on a reasonably-equipped
host:

- `table_rewrite_mb_per_sec = 80.0`
- `gin_build_mb_per_sec = 30.0`

If your hardware is slower (HDD, low-memory VM), halve them in
`config.toml`:

```toml
[upgrade]
table_rewrite_mb_per_sec = 40.0
gin_build_mb_per_sec = 15.0
```

To calibrate accurately, time a migration on a clone:

1. `pg_dump` your database; restore to a separate host.
2. Note the `messages` table size and current time.
3. Run `localmail init-db`. Time it.
4. Solve `time = (table_size_mb + fts_v2_mb) / rate_mb_per_sec` for
   `rate_mb_per_sec` and update your config.

This is overkill for most operators — the defaults are within ~30%
of reality on commodity SSD.

## Why this exists

Migration 0006 was shipped without `CONCURRENTLY` on the two GIN
indexes (the HNSW index in the same migration *is* concurrent).
Per CLAUDE.md, applied migrations can't be edited — so the fix is
not "rewrite 0006" but "give operators a tool to plan around it".
See [GitHub issue #2](https://github.com/hherb/localmail/issues/2)
and the design doc at
[docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md](../superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md).
