# `/admin/imports` — Archive Import Screens (Design)

> **Status:** design approved 2026-06-05. Closes the last 404 admin nav link
> (`/admin/imports`, present in `base.html` since the admin UI shipped).
> Sub-plan label: **2A.5**.

## 1. Purpose

Give an operator a server-rendered admin screen to import an existing email
archive (an **mbox** file or a **maildir** directory) from the localmail host's
filesystem into a pre-created **archive** account, with live progress and
per-message failure isolation.

Nearly all the heavy machinery already exists. The import is essentially:
read raw RFC822 bytes from a file → feed each one through the existing
[`sync.process_one_message`](../../../src/localmail/sync.py) golden path.
Dedup (per-account `message_id` / `raw_sha256`), poison-pill isolation
(`failed_messages`), and content-addressable attachment storage all come for
free. This feature is an **orchestration + UI layer**, not new ingestion logic.

## 2. Scope

**In scope (v1):**
- Formats: **mbox** (single file, many messages) and **maildir** (directory,
  one file per message, possibly with subfolders).
- Source: **server-side path** under a config-driven allowlist. No browser
  upload (maildir is a directory and Gmail-Takeout mbox files are multi-GB).
- Target: a **pre-existing `archive` account**, chosen from a dropdown.
  Operator creates the archive account first via `/admin/accounts` (2A.3).
- Execution: **UI-triggered, in-serve worker thread**. Progress persisted to a
  new `import_jobs` table; surfaced via a self-polling HTMX partial that
  mirrors the daemon panel (2B.5).
- A shared importer core, also exposed as a `localmail import` CLI command.

**Non-goals (deferred):**
- `.eml` / zip upload; browser file upload.
- Importing into live (`password` / `oauth2`) accounts — synthetic import UIDs
  could collide with real IMAP UIDs in the same mailbox, and it muddies
  provenance.
- Explicit per-folder mapping UI (scan-then-map two-step flow).
- Pause/resume bookkeeping — **re-running an import is the resume** (§6).
- Maildir status-flag translation (`flags=[]` in v1). **Received-date IS in
  scope** (§5) — only the IMAP seen/answered *flags* are dropped.

## 3. Architecture

Layers mirror the 2A.3 (accounts) and 2A.4 (users) admin screens exactly: a
transport-free service layer composed by a thin JSON router and a thin HTML
panel router, with pure logic factored into separate unit-tested modules.

| Layer | File | Purpose |
|---|---|---|
| Migration | `migrations/0026_import_jobs.sql` | `import_jobs` table (§4) |
| Pure mailbox reader | `src/localmail/importer/sources.py` | `iter_mbox(path)`, `iter_maildir(path)` → yield `ImportedMessage(mailbox_name, raw_bytes, received_date)`. Read-only; no DB. Includes the pure received-date helpers (§5). |
| Pure path guard | `src/localmail/importer/paths.py` | `resolve_import_path(raw, roots)` → realpath; reject symlink / `..` escape outside the allowlist |
| Importer core | `src/localmail/importer/runner.py` | `run_import(conn_factory, job_id, ...)` — streams a source, calls `process_one_message`, updates counters + `last_progress_at`, checkpoints, honours cancel, and always writes a terminal status (incl. `failed` + `error_msg` on any fatal exception) |
| Service layer | `src/localmail/api/admin/imports.py` | `list_jobs`, `get_job`, `create_job`, `start_job` (spawns worker thread), `cancel_job`; archive-only validation; ACL-scoped |
| Pure form logic | `src/localmail/serve/admin/import_forms.py` | form dict → create-kwargs + error→field map |
| JSON router | `src/localmail/serve/admin/imports_router.py` | `/v1/admin/imports` (machine JSON) |
| HTML panel | `src/localmail/serve/admin/imports_panel_router.py` | `/admin/imports` list + new-import form + `/admin/_partials/import-status/{id}` |
| Templates | `serve/admin/templates/imports/*.html` | `list.html`, `form.html`, `_job_row.html`, `_progress.html` |
| Static | `serve/admin/static/imports-panel.js` | CSP `script-src 'self'`; path-field helpers + post-action toast (daemon-panel pattern) |
| CLI | new `import` command in `cli.py` | `localmail import <path> --account NAME`, reuses `run_import` inline |
| Config | `ImportsConfig` (`[imports]`) in `config.py` | `roots: list[str]`, `checkpoint_every: int = 50` |

### Reused, unchanged
- `sync.process_one_message(conn, *, account_id, mailbox_id, uid, raw, flags, attachments_root, internal_date=None)` — the per-message golden path.
- `sync.upsert_mailbox(conn, account_id, name, delimiter, flags)` — one call per target mailbox.
- `sync.record_failed_message(...)` — poison-pill isolation (already called inside the per-message savepoint pattern; the runner mirrors `sync_mailbox`'s SAVEPOINT-per-message structure).
- Per-account dedup unique indexes (`messages_acct_msgid_uniq`, `messages_acct_rawsha_uniq`).
- Admin CSRF (`serve/admin/csrf.py::csrf_token_for_method`), base layout/nav, ACL resolution (`api/acl.allowed_account_ids`), self-polling HTMX partial pattern (`daemon_panel_router.py`).

## 4. `import_jobs` schema (migration 0026)

```sql
CREATE TABLE import_jobs (
    id               BIGSERIAL    PRIMARY KEY,
    account_id       BIGINT       NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_kind      TEXT         NOT NULL CHECK (source_kind IN ('mbox','maildir')),
    source_path      TEXT         NOT NULL,
    status           TEXT         NOT NULL CHECK (status IN
                        ('pending','running','completed','failed','cancelled')),
    total_messages   BIGINT,                       -- always NULL in v1 (no pre-scan; see §11.2)
    processed        BIGINT       NOT NULL DEFAULT 0,
    inserted         BIGINT       NOT NULL DEFAULT 0,
    skipped_dup      BIGINT       NOT NULL DEFAULT 0,  -- process_one_message did_insert=False
    failed           BIGINT       NOT NULL DEFAULT 0,  -- landed in failed_messages
    error_msg        TEXT,                          -- fatal job error (bad path, unreadable source)
    cancel_requested BOOLEAN      NOT NULL DEFAULT FALSE,
    last_progress_at TIMESTAMPTZ,                  -- heartbeat: bumped each checkpoint while running
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);

-- Busy-guard: at most one active import at a time. Unique on a *constant*
-- expression over the active subset, so any second pending/running row
-- violates it (a unique index on (status) would wrongly permit one pending
-- AND one running simultaneously).
CREATE UNIQUE INDEX import_jobs_single_active_uniq
    ON import_jobs ((TRUE))
    WHERE status IN ('pending','running');
```

Per-message poison pills still land in the existing `failed_messages` table
(recoverable via `localmail retry-failed`); `import_jobs.failed` is only the
running count for display.

## 5. Ingestion mapping

- **mbox** → one mailbox via `upsert_mailbox`, name = operator field (default =
  the file's stem). `mailbox.mbox(path)` yields each message; `.as_bytes()`
  gives the raw RFC822.
- **maildir** → `mailbox.Maildir(path).list_folders()`; each (sub)folder maps to
  its own mailbox preserving the folder name; the root maildir maps to a mailbox
  named after its directory. Each message's `.as_bytes()` is the raw RFC822.
- Per message:
  `process_one_message(conn, account_id=<archive>, mailbox_id=<upserted>,
  uid=<synthetic per-mailbox counter>, raw=<bytes>, flags=[],
  attachments_root=<cfg.attachments.root>, internal_date=<received_date>)`.
  - **`internal_date` carries the archive's received/delivery timestamp.**
    `internal_date` is exactly the "when this arrived at the mailbox" column,
    and the canonical recency ordering already COALESCEs it ahead of
    `date_sent`. Per-format extraction (pure helpers in `sources.py`):
    - **mbox** — the envelope `From ` line
      (`mailbox.mboxMessage.get_from()`), whose date is the conventional mbox
      delivery time. `parse_mbox_from_date(from_line) -> datetime | None`
      tolerantly parses the asctime form (`Fri Jul  8 12:08:34 2011`); the
      naive timestamp is treated as **UTC** (asctime carries no zone — a small,
      documented imprecision). Unparseable / absent → None.
    - **maildir** — `mailbox.MaildirMessage.get_date()` (the message file's
      delivery time, from its mtime), converted to a UTC `datetime`.
    - On a None result the column is left NULL and recency falls back to
      `date_sent` (the `Date:` header) — identical to the `retry-failed` path.
    - Refinement (deferred): scrape the topmost `Received:` header for a
      higher-fidelity delivery time. The `From ` line / file date is the
      conventional, reliable per-format choice for v1.
  - `uid` is a synthetic per-mailbox sequential counter. Archive accounts never
    sync IMAP, so there is no real UID space to collide with.
  - `flags=[]` in v1 (IMAP seen/answered state is not translated; the *date* is).

## 6. Concurrency, cancel, failure visibility, idempotency

- **One import at a time**, enforced two ways: the partial unique index
  `import_jobs_single_active_uniq` (DB-level guarantee) and a service-level
  pre-check that returns a clean **409** rather than surfacing the unique
  violation. Mirrors the daemon supervisor's single-lifecycle-thread busy-guard.
- **Cooperative cancel:** `POST /v1/admin/imports/{id}/cancel` sets
  `cancel_requested=TRUE`. The runner checks it once per checkpoint batch and,
  if set, stops and writes `status='cancelled'`, `finished_at=now()`. Already-
  imported rows remain (valid + deduped).
- **Checkpoint cadence:** the runner updates the counters (`processed`,
  `inserted`, `skipped_dup`, `failed`) and bumps `last_progress_at` every
  `checkpoint_every` messages (default 50, matching `sync_mailbox`'s batch) so
  the polling partial shows live movement and a crash loses at most one batch of
  *counter* progress (the message rows themselves are committed per batch).

### Failure visibility (a job that dies mid-import must surface to the operator)

Three independent layers, all in v1:

1. **Runner terminal status (primary).** The entire `run_import` body is wrapped
   in `try/except`. Any fatal exception — bad/unreadable path, mid-stream source
   corruption, unexpected error — sets `status='failed'`,
   `error_msg=<exception class: message>`, `finished_at=now()` in its own
   committed transaction (separate from the per-message work, so the failure
   record survives a rolled-back batch). The list + detail screens render
   `error_msg` for any failed job. This covers every error the worker thread can
   observe.
2. **Liveness / stall detection.** Because `last_progress_at` is bumped each
   checkpoint, the panel flags a `status='running'` job as **stalled** (red, "no
   progress for Ns") once `now() - last_progress_at > [imports].stale_seconds`
   — the same server-computed stale flag the daemon panel uses (no client
   clock). This catches a hard worker-thread death that never reached the
   `except` (e.g. the process is killed, or a thread is terminated) *before* a
   restart.
3. **Startup reconciliation.** On serve startup the lifespan runs
   `UPDATE import_jobs SET status='failed', error_msg='interrupted: serve
   process restarted', finished_at=now() WHERE status IN ('pending','running')`.
   An in-serve worker cannot survive a process restart, so any still-active job
   at startup is by definition orphaned. This guarantees a crashed import is
   marked failed (not stuck `running` forever) and clears the busy-guard for the
   next import.

- **Re-import is idempotent:** per-account `message_id` / `raw_sha256` dedup
  means re-running a failed or cancelled import skips already-imported messages
  (counted as `skipped_dup`). This is the resume story — no offset bookkeeping.

## 7. Security

- **Path allowlist:** `resolve_import_path(raw, roots)` resolves the input to a
  realpath and requires it to live under one of the configured `[imports].roots`
  entries (each also realpathed). Symlink escape and `..` traversal are rejected
  → **400**. An **empty `roots` disables imports**: the panel renders a
  configuration notice and the start action is unavailable — the same opt-in
  pattern as the admin signing keys.
- **Archive-only target:** the service rejects non-`archive` accounts (mirrors
  `probe_connection`'s archive rejection) → **400**.
- **ACL:** the target account must be in the caller's `allowed_account_ids`;
  every service accessor takes the keyword-only `allowed_account_ids: list[int]`
  like the rest of `api/admin/*`.
- **CSRF:** method-bound tokens (`csrf_token_for_method`) on every mutating
  action (start, cancel), so a token minted for one verb can't replay on
  another.
- The `/v1/admin/*` JSON router stays pure machine-JSON; the panel polls the
  dedicated HTML partial (no content negotiation), per the 2B.5 convention.

## 8. HTTP surface

JSON (`/v1/admin/imports`, admin-gated, method-bound CSRF):
- `GET /v1/admin/imports` — list jobs (ACL-scoped).
- `GET /v1/admin/imports/{id}` — one job.
- `POST /v1/admin/imports` — create + start a job (body: account_id, source_kind,
  source_path, optional mailbox name override). **409** if an import is active;
  **400** on bad path / non-archive / out-of-allowlist; **403**/**404** on ACL.
- `POST /v1/admin/imports/{id}/cancel` — request cancel.

HTML (`/admin/imports`, admin-gated):
- `GET /admin/imports` — list + new-import form (account dropdown of archive
  accounts; path field; source-kind; optional mailbox-name override). Disabled
  with a notice when `[imports].roots` is empty.
- `POST /admin/imports` — start (HX-Redirect to the job's progress page on
  success; inline field error on 400/409).
- `GET /admin/imports/{id}` — job detail with the live progress partial; shows
  `error_msg` when the job is `failed`.
- `GET /admin/_partials/import-status/{id}` — self-polling fragment
  (`hx-get` + `hx-trigger="every 2s"`, re-carried after each swap; stops at a
  terminal status). Red styling for `failed` **and for a stalled `running` job**
  (server `stale` flag, §6); shows `error_msg` and counters for processed /
  inserted / skipped-dup / failed.
- `POST /admin/imports/{id}/cancel` — cancel button (method-bound CSRF).

## 9. Configuration

```toml
[imports]
# Directories the import UI may read archives from. Empty = imports disabled.
# Each path is realpathed; an import source must resolve to a location under
# one of these roots (symlink / .. escape rejected).
roots = ["/srv/localmail/imports"]
# How often (in messages) the runner flushes progress counters + last_progress_at.
checkpoint_every = 50
# A running job whose last_progress_at is older than this is shown stalled (red).
stale_seconds = 60
```

`ImportsConfig` is a new pydantic model on `Config` (`imports: ImportsConfig =
Field(default_factory=ImportsConfig)`), following `SearchConfig` / `AuthConfig`.
No magic numbers live in importer code.

## 10. Testing (TDD)

**Pure units (no DB):**
- `sources.py`: mbox + maildir iteration over fixtures built programmatically in
  the test (no mbox/maildir/`.eml` files checked in — repo convention). Includes
  a maildir with a subfolder (asserts per-folder mailbox names) and an empty
  source. Asserts each yielded `ImportedMessage` carries the expected
  `received_date`.
- Received-date helpers: `parse_mbox_from_date` over a table of asctime
  `From ` lines (valid → UTC datetime; malformed/absent → None) and the maildir
  `get_date()` → UTC conversion.
- `paths.py`: allowlist table — inside-root accepts; outside-root, `..`
  traversal, symlink-escape, and empty-roots reject.
- `import_forms.py`: form → kwargs + error→field mapping.
- The busy-guard predicate, the `status` transition helper, and the `stale`
  predicate (`now - last_progress_at > stale_seconds` only while `running`).

**Service tests (real `localmail_test` DB):**
- create/list/get job; archive-only rejection (live account → 400); ACL scoping
  (foreign account → not visible / rejected); dedup-on-reimport increments
  `skipped_dup`; cancel flips `status='cancelled'`; busy-guard second-start 409;
  `ON DELETE CASCADE` removes jobs with the account.

**Route tests:**
- JSON `/v1/admin/imports`: CRUD-ish happy paths, 400/409/403 mappings,
  method-bound CSRF rejection.
- HTML panel: renders list, form disabled when `roots` empty, progress partial
  re-carries its poll trigger and stops at terminal status.

**Runner end-to-end (real DB):**
- Small in-memory mbox → message rows land in the archive account, attachments
  stored, `inserted` count correct, **`internal_date` equals the parsed
  `From `-line received date** (and NULL when the line is unparseable).
- A poison message in the stream → lands in `failed_messages`, `failed` count
  increments, surrounding messages still imported (savepoint isolation).
- Re-run the same source → all `skipped_dup`, no new rows.
- Cancel mid-run (inject `cancel_requested`) → stops, `status='cancelled'`,
  partial rows valid.
- **Fatal source error** (unreadable path / corrupt source) → `status='failed'`
  with a non-empty `error_msg`, `finished_at` set, busy-guard cleared.
- **Startup reconciliation**: seed a `running` job, run the reconcile step →
  job becomes `failed` with the "interrupted" `error_msg`; a `completed` job is
  untouched.

## 11. Open decisions / risks

1. **Worker thread lives in the serve process.** A large import competes with
   request handling, but `localmail serve` is low-traffic operator-facing. One
   import at a time bounds the load. If this proves heavy, a later slice can
   move the runner to a subprocess (the importer core already takes a
   `conn_factory`, so the move is mechanical).
2. **`total_messages` pre-scan — decided: v1 does NOT pre-scan.** Counting an
   mbox means a first pass over the file, doubling IO on multi-GB archives. v1
   leaves `total_messages` NULL and the UI shows indeterminate progress ("N
   processed", with `inserted` / `skipped_dup` / `failed` counts) rather than a
   percentage. A pre-scan that fills `total_messages` (and enables a progress
   bar) is a clean later follow-up — the column already exists.
3. **No new migration conflicts:** latest applied is `0025`; this adds `0026`.
   Re-check `ls migrations/` at plan time.
4. **maildir seen/answered flags** are dropped in v1 (`flags=[]`). Received-date
   *is* carried (§5); only the IMAP flag state is omitted. A later slice can
   translate maildir `info` flags if operators need read/unread state.
5. **`internal_date` semantics.** Imports store a parsed archive delivery
   timestamp in `internal_date` (the "arrived at mailbox" column), treated as
   UTC when the source carries no zone (mbox `From ` asctime). This is a
   deliberate, documented minor imprecision; a `Received:`-header parser is the
   higher-fidelity refinement (deferred, §5).
6. **Crash visibility is handled in v1** via the three-layer mechanism in §6
   (runner terminal status + `last_progress_at` stall flag + startup
   reconciliation). No job can be left silently stuck `running`.
