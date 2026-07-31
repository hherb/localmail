# Ingestion edge cases: synthetic UID allocation + degenerate Message-Id + empty BODY[]

**Date:** 2026-08-01
**Issues:** [#215](https://github.com/hherb/localmail/issues/215) (High),
[#222](https://github.com/hherb/localmail/issues/222) A + B (Low / Low-Medium)
**Status:** approved, implementing

Three narrow ingestion defects, all against the same intent: *never silently
collapse two distinct messages, and never make a skip unrecoverable*.

## Background

`message_labels` carries `UNIQUE (mailbox_id, uid)` (migration `0001_init.sql`).
For IMAP-sourced mail the UID is the server's truth and that constraint is a
faithful model of RFC 3501. For **archive imports** the UID is synthetic —
invented by `importer/runner.py` — and the current allocator does not respect
the namespace it is writing into.

`message_labels.uid` is **read by nothing**. Search (`search/arms.py`), browse
(`api/browse.py`), account listing (`api/accounts.py`) and message fetch
(`api/messages.py`) all reference `mailbox_id` only. The uid exists solely to
satisfy the unique constraint and as the `failed_messages` retry key. This is
what makes re-allocation a safe repair: no consumer observes the value.

## A. #215 — synthetic UID collision poison-pills legitimate messages

### Defect

`run_import` restarts a positional counter at 1 for every run, keyed on the
source's mailbox name. `mailbox_name` is the mbox `path.stem` (or the maildir
basename), and `upsert_mailbox` resolves idempotently on `(account_id, name)`.
So `2023/Inbox.mbox` and `2024/Inbox.mbox` imported into the same archive
account resolve to the **same `mailbox_id`** and the second run re-issues
`uid = 1, 2, 3, …` over UIDs the first run already committed.

`upsert_label`'s `ON CONFLICT (message_id, mailbox_id) DO UPDATE` arbitrates the
primary key only — it does not cover `(mailbox_id, uid)`. A genuinely-new
message assigned a recycled UID therefore raises `UniqueViolation`, is caught by
the per-message SAVEPOINT, and lands in `failed_messages` **not because it is
malformed but purely from the recycled UID**.

It is not self-healing: `retry-failed` replays the stored `(mailbox_id, uid)`
and collides again, forever.

### Fix

Continue from `MAX(uid) + 1` for the target mailbox, resolved once per mailbox
per run at first touch:

```
run 1 (2023/Inbox.mbox, 500 msgs):  uid 1 .. 500
run 2 (2024/Inbox.mbox, 300 msgs):  uid 501 .. 800   <- no collision
re-run of the 2023 file:            uid 801 .. 1300  <- dedups; uids churn up
```

Collision-free by construction: every UID issued in a run exceeds every UID
already stored for that mailbox, so an INSERT never conflicts and a `DO UPDATE`
always moves an existing row to an unused value. Message-level dedup is
unaffected — a re-imported file still reports `skipped_dup`; only the label's
uid churns upward, which nothing reads.

Rejected alternatives:

- **Content-derived UID** (truncated hash of the raw bytes) is genuinely
  idempotent, but a hash collision is permanent unrecoverable poison — the exact
  failure mode being fixed — and a non-ascending "UID" contradicts IMAP.
- **One mailbox per source file** sidesteps collisions but changes user-visible
  folder names and duplicates a folder on every re-import.

### Recovery for rows already poisoned

Fixing the allocator stops new collisions but leaves existing `failed_messages`
rows failing forever. `retry_failed_messages` therefore joins `accounts` and,
for **archive accounts only**, allocates a fresh UID instead of replaying the
stored one:

> Synthetic UIDs are re-allocated on retry. Real IMAP UIDs are preserved
> verbatim.

`localmail retry-failed` becomes the documented recovery path; no DB surgery.

Rejected: making `upsert_label` itself collision-tolerant would fix import,
retry, *and* the uidvalidity-reset resync in one place, but it would silently
paper over a genuine IMAP invariant violation on the sync path — hiding a real
server or schema bug.

## B. #222B — blank-but-present Message-Id collapses distinct messages

### Defect

Dedup falls back to `raw_sha256` only when `message_id IS NULL`. A header that
is present but degenerate (`Message-Id: <whitespace>` from a broken MTA) yields
a non-None, non-unique string, so two distinct messages sharing it in one
account collapse onto one `messages` row — the second message's subject, body
and attachments are discarded and only its `message_labels` row is added.

`parse_message` currently does `str(message_id) if message_id else None`.

**Corrected during implementation.** The issue describes the trigger as
`Message-Id: <whitespace>`, which is ambiguous. Probing the parser showed the
whitespace-only case was **already safe**: `email.policy.default` collapses a
header body of `"   "` or `"\t"` to `""`, which the `if message_id` guard
catches. The form that actually reaches the DB intact is the **empty
angle-addr** — `<>`, `< >`, `<\t>` — which parses to a truthy, non-unique
string. The first version of the DB regression test used a whitespace-only
header and passed with the fix reverted; switching the fixture to `<>` made it
fail with `{'INBOX': 1} == {'INBOX': 2}`, i.e. one message swallowing the other.
The fix covers both classes regardless.

### Fix

A pure `normalize_message_id(value) -> str | None` in `parser.py`: strip
surrounding whitespace; an empty result is `None`; an empty angle-addr (`<>`,
`< >`) is `None`. A degenerate header then falls through to the existing
`raw_sha256` path and the two messages stay distinct.

Stripping also normalises a well-formed value, which is a no-op in practice —
`email.policy.default` already unfolds and strips header values.

Scope: `message_id` only. `in_reply_to` is not a dedup key.

## C. #222A — empty BODY[] skipped with no recoverable record

### Defect

When a UID is in the batch but the FETCH returns no `BODY[]`, the UID is folded
into the watermark with only a WARNING. If the message was genuinely expunged
this is right. If the server hiccupped, the message is **permanently skipped**
with no retry path — unlike the poison-pill branch directly below it, which
records to `failed_messages`.

`failed_messages` is structurally the wrong home: `raw_bytes` is `NOT NULL` and
`retry_failed_messages` re-parses the stored bytes, so recording an empty body
there would either fail forever or insert a bogus empty message.

### Fix

Distinguish the two cases at the point of the skip with one targeted probe:

```python
raw = data.get(b"BODY[]")
if not raw:
    if _uid_still_on_server(imap, uid):      # search(["UID", f"{uid}:{uid}"])
        log.warning("transient empty body; holding watermark")
        hold_at = uid if hold_at is None else min(hold_at, uid)
    else:
        log.info("UID %s expunged; skipping", uid)
        highest_seen = max(highest_seen, uid)
    seen += 1
    continue
```

- **Gone from the mailbox** → genuinely expunged. INFO, advance normally. This
  is the common case (mail deleted between SEARCH and FETCH) and it must not
  hold anything back, or the watermark would pin forever.
- **Still present**, or the probe itself raises → treat as transient. WARNING,
  and record `hold_at`.

A held-back UID must survive the rest of the run: `highest_seen` is a running
max, so a later UID in the same chunk would otherwise carry the watermark past
the stuck one. Every checkpoint therefore clamps through the pure
`checkpoint_uidnext(highest_seen, hold_at)`. The batch keeps processing (later
messages are still ingested); only the resume point is held, so the next run
re-fetches from the stuck UID.

Cost: while a transient persists, that run's tail is re-fetched on the next run.
Dedup makes the re-inserts cheap. A *persistently* empty-but-present UID (a
server bug) loops — and emits a WARNING every run, which is the intended loud
signal.

No migration, no new table, no bogus raw bytes.

## New module: `src/localmail/uids.py`

Everything about UID numbering, in one focused place. Shared by
`importer/runner.py` and `sync.py`; `sync.py` is already 771 lines and should
not grow.

| symbol | kind | contract |
|---|---|---|
| `ARCHIVE_AUTH_METHOD` | const | `"archive"` — no bare literal at the new call sites |
| `next_uid_after(max_uid)` | pure | `(max_uid or 0) + 1` |
| `should_reallocate_uid(auth_method)` | pure | `auth_method == ARCHIVE_AUTH_METHOD` |
| `checkpoint_uidnext(highest_seen, hold_at)` | pure | `highest_seen + 1`, clamped to `hold_at` |
| `max_label_uid(conn, mailbox_id)` | thin IO | `SELECT COALESCE(MAX(uid), 0) FROM message_labels WHERE mailbox_id = %s` |

`conn` is annotated so mypy enforces the `fetchone()` null check.

## Testing

Built test-first; every test watched fail before the implementation lands.

- `tests/test_uids.py` — the four pure functions, including the `hold_at is
  None` passthrough and the clamp.
- `tests/test_import_uid_collision.py` (DB) — two sources sharing a stem into
  one archive account ingest completely with **zero** `failed_messages`; and a
  pre-seeded colliding `failed_messages` row is recovered by `retry-failed`
  while a non-archive row keeps its stored UID.
- `tests/test_parser.py` — degenerate Message-Id variants normalise to `None`;
  a well-formed one is untouched.
- DB test — two distinct messages carrying the same blank Message-Id produce
  **two** `messages` rows.
- `tests/test_sync.py` — the expunged branch advances the watermark; the
  transient branch holds it and the next run ingests the message successfully.

`tests/_fake_imap.py` gains two knobs (it is the only place to extend when sync
needs new IMAP behaviour):

- `suppress_body: set[int]` — UID is visible to SEARCH but FETCH returns no
  `BODY[]` → the transient branch.
- `phantom_uids: set[int]` — UID appears in the ALL/range sweep but is absent
  from the mailbox, so the targeted probe returns empty → the expunged branch.

The probe uses the closed-range form `f"{uid}:{uid}"` rather than a bare UID:
both are valid IMAP, and the closed form is unambiguous.

## Out of scope

- **UIDVALIDITY-reset collisions.** After a reset, `sync_mailbox` clears labels
  and resyncs, but new server UIDs can in principle collide with rows that
  survive elsewhere. A separate concern from synthetic-UID allocation.
- **`in_reply_to` normalisation** — not a dedup key.
- **Retro-repair of existing `messages` rows** already collapsed by a degenerate
  Message-Id. The fix is prospective; a collapsed pair cannot be recovered
  without the discarded bytes.
