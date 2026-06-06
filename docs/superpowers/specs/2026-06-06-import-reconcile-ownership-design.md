# Ownership-aware import reconcile (#162)

> Status: approved design, 2026-06-06. Implements the fix for GitHub issue
> [#162](https://github.com/hherb/localmail/issues/162) — "imports: serve-startup
> reconcile can wrongly fail a concurrent CLI import".

## Problem

`api.admin.imports.reconcile_orphaned_jobs` runs at serve startup
([serve/app.py](../../../src/localmail/serve/app.py)) and marks **every**
`pending`/`running` `import_jobs` row `failed`, on the assumption that an active
row can only be an orphaned in-serve worker thread.

But `localmail import` runs the same `run_import` core **synchronously in a
separate process** and also creates a `running` `import_jobs` row. If the serve
process (re)starts while a CLI import is in flight:

1. The CLI's job is flipped to `failed`
   (`error_msg = 'interrupted: serve process restarted'`) even though the worker
   is alive and still importing.
2. The global single-active busy-guard (`import_jobs_single_active_uniq`) is
   released, so a panel-initiated import can start **concurrently** with the
   still-running CLI job — violating the single-active invariant.

It self-heals cosmetically (the CLI worker's final `_mark_terminal` overwrites
`failed` → `completed`), and per-account Message-Id / raw-SHA256 dedup keeps the
data safe, but the concurrent-import window is real.

Severity is low (requires a CLI import concurrent with a serve restart against
the same DB; data integrity is preserved by dedup), but the single-active
invariant and the job-status display are both affected.

## Constraints / context

- The busy-guard `import_jobs_single_active_uniq` means there is **at most one
  active (`pending`/`running`) row at any time**, so reconcile only ever
  examines one row. The per-row liveness check is therefore trivially cheap.
- Both writers of an active row run the import **in the same process that
  created the row**:
  - **CLI** (`localmail import`): `create_job` then `run_import`, synchronously,
    one process.
  - **Serve panel / JSON router**: `create_job` in the request handler, then
    `start_job` spawns the worker as a thread **in the same serve process**.
- Single-host is the project model (no multi-host clustering — explicit
  non-goal).

## Chosen approach: `owner_host` + `owner_pid` (Approach B)

Every `import_jobs` row records the host + pid of the process that created it.
Reconcile reaps an active row only when its owner is gone: `owner_host ==
this_host AND the pid is no longer alive`.

This is the complete fix: it reaps a genuinely orphaned job whether the **serve**
process crashed (serve's old pid dead) **or** a **CLI** process crashed (CLI pid
dead), while a **live** CLI import (pid alive) survives a serve restart.

### Rejected alternative: `supervised BOOLEAN` (Approach A)

Serve-thread jobs `supervised = TRUE`, CLI jobs `FALSE`; reconcile reaps only
`supervised = TRUE` rows. Simpler (no pid check), but a CLI import whose process
genuinely crashed is `supervised = FALSE` and is **never** reaped — its row
blocks the busy-guard forever. That is a regression versus today (where the next
serve restart clears it). Rejected.

## Design

### 1. Schema — migration `0027_import_jobs_owner.sql`

Add two **nullable** columns:

```sql
ALTER TABLE import_jobs ADD COLUMN owner_host TEXT;
ALTER TABLE import_jobs ADD COLUMN owner_pid  INTEGER;
```

Nullable, no default — the migration is back-compatible: any pre-existing active
row has `NULL` owner. New inserts always populate both. No index — there is at
most one active row, so reconcile's scan is trivial.

### 2. Recording ownership — at `create_job` time

`create_job` records `socket.gethostname()` + `os.getpid()` on the INSERT. This
is correct for both paths because the process that *creates* the row is the same
process that *runs* it. Recording at create (not `_mark_running`) means even a
`pending` row carries an owner, so reconcile treats every active row uniformly.

The `ImportJob` dataclass and the `_SELECT` projection gain `owner_host`,
`owner_pid` fields (consistent with the existing `class_row` name-mapping).

### 3. Reconcile logic — pure predicate + thin IO

New pure module **`importer/ownership.py`**:

```python
def pid_is_alive(pid: int) -> bool:
    """os.kill(pid, 0): returns normally -> alive; ProcessLookupError -> dead;
    PermissionError -> alive but not ours (still alive)."""

def should_reap(*, owner_host, owner_pid, current_host, pid_alive) -> bool:
    """Reap an active row iff its owner is gone:
      - owner_pid is None          -> reap (legacy/pre-0027, or never-started)
      - owner_host != current_host -> keep (another host's job; not ours to judge)
      - else                       -> reap iff not pid_alive
    """
```

`should_reap` is a pure predicate (host/pid in, bool out — no syscalls), fully
unit-tested. `pid_is_alive` is the one syscall, isolated and tested separately.

`reconcile_orphaned_jobs(conn, *, current_host=None, pid_alive=pid_is_alive)`
becomes:

1. `SELECT id, owner_host, owner_pid` over active rows.
2. For each, compute `should_reap(owner_host, owner_pid, current_host,
   pid_alive=pid_alive(owner_pid) if owner_pid is not None else False)`.
3. Batch the reap UPDATE (same `failed` + `error_msg = 'interrupted: serve
   process restarted'` + `finished_at = now()` write as today) for the reaped
   ids.
4. Return the reaped count.

`current_host` defaults to `socket.gethostname()`; `current_host` and
`pid_alive` are injectable so the DB test is deterministic (no real pids /
hostnames required).

### 4. Edge cases / accepted limitations

- **NULL owner** (legacy active rows from before 0027, or a row that crashed
  between INSERT and commit) → reaped. Correct: unverifiable, and a deploy
  restart means they are orphans.
- **pid reuse**: a dead import's pid reused by an unrelated live process → row
  wrongly kept → stuck until the next restart. Low probability on single-host;
  self-heals. Documented, not engineered around.
- **other-host rows** (`owner_host != current_host`): kept, never reaped.
  Single-host is the project model, so this is defensive; we do not reap what we
  cannot verify.

### 5. Net behavioural change

A CLI import in flight (pid alive) now survives a serve restart's reconcile —
the busy-guard stays held, so no concurrent panel import can start. Orphaned
serve **and** CLI jobs (pid dead) are still reaped.

## Testing (TDD)

- `tests/test_importer_ownership.py` (new) — pure `should_reap` table (alive →
  keep, dead → reap, NULL owner → reap, foreign host → keep) + `pid_is_alive`
  (own pid alive; a definitely-dead pid → dead).
- `tests/test_api_admin_imports.py` (existing — holds the `reconcile_orphaned_jobs`
  tests) — DB-level: live-pid row survives, dead-pid row reaped, NULL-owner row
  reaped, return-count correct, foreign-host row survives; `create_job`
  populates `owner_host` / `owner_pid`. Inject `current_host` + `pid_alive` for
  determinism. Update the existing reconcile test(s) for the new selective
  behaviour.
- `tests/test_import_jobs_schema.py` (existing) — assert the `owner_host` /
  `owner_pid` columns exist after migration.

## Files touched

- `migrations/0027_import_jobs_owner.sql` (new)
- `src/localmail/importer/ownership.py` (new — pure `should_reap` + `pid_is_alive`)
- `src/localmail/api/admin/imports.py` (`ImportJob` fields, `_SELECT`,
  `create_job` records owner, `reconcile_orphaned_jobs` selective reap)
- `tests/test_importer_ownership.py` (new)
- `tests/test_api_admin_imports.py` (DB-level reconcile + create_job owner assertions)
- `tests/test_import_jobs_schema.py` (owner-column existence)
- `CLAUDE.md` (schema-essentials + imports notes), `README.md` if user-facing
