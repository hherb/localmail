# Language-detection starvation (#251) — design

**Status:** approved 2026-08-05. Closes
[#251](https://github.com/hherb/localmail/issues/251).

## The defect

`search.lang_detect.run_lang_detect_pass` returns 0 on every call, forever, on
both deployments. `messages.body_lang` detection has been stopped archive-wide
for weeks.

| host | `body_lang_populated` | `body_lang_pending` |
|---|---|---|
| Mac | **7744** (frozen across daemon restarts and repeated sweeps) | 100020, growing |
| DGX | **8324** (unchanged since the session-13 handoff) | 21149, growing |

The claim query is

```sql
SELECT id, body_text FROM messages
 WHERE body_lang IS NULL AND body_text IS NOT NULL
 ORDER BY id
 LIMIT %s
FOR UPDATE SKIP LOCKED
```

A row the detector **declines** — below `body_lang_min_text_chars`, below
`body_lang_min_confidence`, or a body that makes the detector raise — stays
`body_lang IS NULL`. It therefore still satisfies the predicate, and under a
stable `ORDER BY id` it is re-claimed in **the same position** on every
subsequent sweep. Once the first `body_lang_detect_batch_size` rows are all
unlabelable, the head of the queue is permanently occupied and nothing behind
it is ever reached.

On the live Mac archive 145 of the head 200 rows are under the 20-char floor
and the remainder are separator blocks; all 200 are re-read every sweep and
`updated` is 0.

This is the same shape as #216, where a turned-away blob gained no row, stayed
eligible, and re-filled the claim batch forever.

### The second half, which the issue does not name

`run_lang_detect_pass` returns **rows labelled**, not rows visited, and both
the `lang-backfill` and `embed-backfill` drain loops break on 0. That return
value was itself a deliberate earlier fix — pinned by
`test_run_lang_detect_pass_loop_terminates_on_persistent_null` — to stop those
loops spinning forever on archives full of unlabelable bodies.

So making the claim skip declined rows is **necessary but not sufficient**: a
batch that declines every row would still return 0 while having made real
progress, and both loops would still stop early. Both halves have to move
together, which is why the return type changes in the same commit.

### Blast radius

`body_lang` backs the `lang:` search DSL token and the `/v1/search?languages=`
filter. Every message after the first unlabelable batch is unlabelled, so
`lang:` silently matches a small, arbitrary, oldest-first subset of the
archive. Search returns wrong results rather than erroring.

Archive-wide on the Mac, **98,189 of 100,020 pending rows clear the length
floor** — the fix recovers nearly the whole archive, not a sliver.

## Decision: a column, not a sentinel

`body_lang` itself cannot carry "we tried and declined", because NULL must keep
meaning "unknown" for the `lang:` filter. The state has to live somewhere else.

**Chosen: `messages.body_lang_attempted_at TIMESTAMPTZ` (migration 0035).**
`body_lang` keeps its exact meaning, so no reader changes; re-opening declined
rows after loosening detector policy is one UPDATE.

**Rejected: a sentinel language value (`'und'`).** It needs no migration, but
it changes what `body_lang IS NULL` means and four readers must learn to
exclude it — `arms.py`'s `lang:` filter, `searcher._maybe_warn_unpopulated_body_lang`
(which would stop warning once any `'und'` existed), `search-status`'s
`body_lang_populated` count, and migration 0015's index. It also repeats
#216's one-way door, which CLAUDE.md already documents as a trap: lowering
`body_lang_min_confidence` would silently *not* re-open the rows it was
lowered for.

**Rejected: a rotating in-memory claim watermark.** No schema change, but the
watermark is per-process state — a daemon restart resets it to the head and
re-reads the dead prefix — and it records nothing, so `search-status` still
could not tell an operator how many rows are genuinely unlabelable.

## Schema

`migrations/0035_messages_body_lang_attempted_at.sql`:

```sql
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS body_lang_attempted_at TIMESTAMPTZ;

DROP INDEX IF EXISTS messages_body_lang_pending_idx;

CREATE INDEX IF NOT EXISTS messages_body_lang_claimable_idx
    ON messages (id)
    WHERE body_lang IS NULL
      AND body_text IS NOT NULL
      AND body_lang_attempted_at IS NULL;
```

**The index is replaced under a new name, deliberately.** Migration 0017's
`messages_body_lang_pending_idx` carries the old claim predicate verbatim.
`CREATE INDEX IF NOT EXISTS` matches on **name only**, so recreating that name
with a new predicate would silently no-op on any host that already has it,
leaving the worker on an index that no longer matches its claim.

**Rows labelled before this migration keep a NULL `attempted_at`.** That state
is legal and never consulted: the claim excludes `body_lang IS NOT NULL`
first. No backfill of already-labelled rows is needed.

**Lock cost.** `ADD COLUMN` nullable-with-no-default is metadata-only in
Postgres 11+. The index build takes a write lock for a few seconds on the
127k-row Mac archive. This is not `estimate-upgrade` territory and gets no
entry in `ESTIMATORS`; the migration comment states the cost instead.

## One authority for the predicate

The claim query, `search-status`'s counter, and the migration's index
predicate must agree. A drift makes `search-status` report work the worker
will never claim, or hide work it will — which is exactly how this bug stayed
invisible. Two module constants in `search/lang_detect.py`, following the
pattern of `api/browse.py::build_where` and
`api/revocation_sql.py::credential_valid_sql`:

```python
CLAIMABLE_WHERE_SQL = ("body_lang IS NULL AND body_text IS NOT NULL "
                       "AND body_lang_attempted_at IS NULL")
DECLINED_WHERE_SQL  = ("body_lang IS NULL AND body_text IS NOT NULL "
                       "AND body_lang_attempted_at IS NOT NULL")
```

A test asserts the two are disjoint and together cover
`body_lang IS NULL AND body_text IS NOT NULL`, so neither can drift into
overlapping or into leaving a gap.

They live in `lang_detect.py` rather than a new top-level pure module because
there is no config import cycle to break here — unlike `account_names.py`,
`ocr_policy.py`, and `fetch_retry.py`, which sit at the top level precisely
because `config.py` imports them.

## `run_lang_detect_pass`

### Return type

```python
@dataclass(frozen=True)
class LangDetectPass:
    visited: int    # rows claimed this call
    labelled: int   # rows that gained a body_lang
```

Drain loops terminate on `visited == 0`; progress reporting uses `labelled`.

**No `__bool__`.** `if not result:` reads ambiguously, and an implicit reading
of this exact return value is what caused the bug. Callers write
`result.visited == 0` and say which question they are asking.

### Write path

Every claimed row gets one uniform write:

```sql
UPDATE messages SET body_lang = %s, body_lang_attempted_at = now() WHERE id = %s
```

with `%s` possibly NULL. Declining and labelling take the same statement, so
no future branch can label a row without stamping it — the same
by-construction reasoning as #249's `ExtractedText.__post_init__` and #67's
unconditional ACL check.

### The poison path

On a detector exception the existing code does `ROLLBACK TO SAVEPOINT`, which
would discard the stamp along with everything else — so a body that reliably
crashes the detector would re-wedge the head of the queue exactly as a
declined body does today. A second, separately-savepointed
`_mark_attempted_safely(cur, mid)` writes the stamp **after** the rollback.

Its `SAVEPOINT` statement sits **outside** its `try`, so `ROLLBACK TO` is
always valid — the same shape as `sync.record_failed_message` and
`fetch_retry.record_attempt`. If the stamp itself fails, that is logged and
the row stays claimable; a persistent failure to write one column is a broken
database, and re-attempting is the safe direction.

## Callers

- **`lang-backfill`** and **`embed-backfill`**: loops break on
  `result.visited == 0` instead of `result.labelled == 0`, and report both
  numbers.
- **`lang-backfill --retry-declined`**: runs
  `UPDATE messages SET body_lang_attempted_at = NULL WHERE <DECLINED_WHERE_SQL>`
  before draining. This is what makes the column strictly better than the
  sentinel — #216's one-way door becomes a discoverable command instead of a
  documented SQL snippet an operator has to find. It sits **after** the
  existing `body_lang_enabled` guard: with detection disabled the command
  still says "nothing to do" and re-opens nothing, since re-opening rows
  nothing will then process only inflates `body_lang_pending`.
- **`search-status`**: gains `body_lang_declined`, and `body_lang_pending`
  switches to the *claimable* predicate.

**Redefining `body_lang_pending` is deliberate.** Today it reports 100,020
rows that will never be processed — the lie that hid this bug for weeks. After
the change, `pending` means work remaining and `declined` means the
genuinely-unlabelable remainder, and the two together still account for every
NULL-with-a-body row.

## Testing

TDD; the failing test is watched first in each case.

**The regression test** is `test_advances_past_an_undetectable_head`: seed a
batch's worth of unlabelable rows followed by labelable ones, with
`batch_size` smaller than the total. Pass 1 visits the head and labels 0; pass
2 labels the rest. Against the current implementation pass 2 labels 0.

Also:

- declined rows are stamped and not re-claimed
- poison rows (detector raises) are stamped and not re-claimed
- labelled rows are stamped too (the uniform write path)
- `visited` and `labelled` are reported separately
- `--retry-declined` re-opens declined rows and leaves labelled rows alone
- `CLAIMABLE_WHERE_SQL` and `DECLINED_WHERE_SQL` partition the pending set
- the `lang-backfill` CLI drains an archive whose head is unlabelable
- `search-status` reports `body_lang_declined`, and `body_lang_pending` counts
  claimable rows only
- `messages_body_lang_claimable_idx` exists after migration and
  `messages_body_lang_pending_idx` is gone

`test_run_lang_detect_pass_loop_terminates_on_persistent_null` is **corrected,
not deleted**. Termination now comes from `visited` reaching 0 on the second
pass, which is the invariant that should always have been asserted; the
labelled-count reading of it is what traded a spinning loop for a starving
one.

## Deployment

The fix is inert until `init-db` applies 0035 on each host. After that the
backlog drains at `body_lang_detect_batch_size` per embed-worker sweep, or in
one pass via `localmail lang-backfill`.

Expected on the Mac: `body_lang_pending` 100,020 → ~0, `body_lang_populated`
7,744 → ~98k, `body_lang_declined` → the ~2k genuinely unlabelable remainder.

## Documentation

- `CLAUDE.md` — the schema-essentials section gains `body_lang_attempted_at`
  and the claim invariant; the migrations line moves to `0035`, next free slot
  `0036`.
- `README.md` — the `lang-backfill` entry gains `--retry-declined`, and the
  `search-status` field list gains `body_lang_declined` with the redefinition
  of `body_lang_pending` stated.

## Out of scope

- No change to detector policy — `body_lang_min_confidence`,
  `body_lang_min_text_chars`, and `body_lang_low_accuracy` keep their defaults.
  This fix makes the queue advance; whether the floors are set well is a
  separate question the new `body_lang_declined` counter finally makes
  measurable.
- No failure table for poison bodies. Persistent detector failures surface as
  repeated WARNINGs, matching the policy already in place for attachment
  chunking.
