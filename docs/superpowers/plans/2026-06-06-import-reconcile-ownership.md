# Ownership-aware Import Reconcile (#162) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make serve-startup `reconcile_orphaned_jobs` reap only genuinely orphaned import jobs (a dead pid on this host, or a NULL owner), so a live `localmail import` running in another process survives a serve restart and keeps the single-active busy-guard held.

**Architecture:** Add `owner_host` + `owner_pid` to `import_jobs`, recorded at `create_job` time (the creating process is the running process for both CLI and serve-thread paths). A new pure module `importer/ownership.py` holds the orphan-decision predicate `should_reap` (no syscalls — unit-tested) and the single liveness syscall `pid_is_alive`. `reconcile_orphaned_jobs` selects active rows, computes `should_reap` per row, and batch-reaps only the dead ones.

**Tech Stack:** Python 3.12, psycopg v3 + raw SQL, numbered `.sql` migrations, pytest against a real `localmail_test` Postgres.

**Spec:** [docs/superpowers/specs/2026-06-06-import-reconcile-ownership-design.md](../specs/2026-06-06-import-reconcile-ownership-design.md)

**Conventions reminder:** No comments unless the WHY is non-obvious. No magic numbers. `assert row is not None` before any `fetchone()[0]` (mypy is enabled). New SQL goes in a new numbered migration — never edit an applied one. Prefix ad-hoc `uv run` with `unset VIRTUAL_ENV &&`.

---

## Task 1: Migration 0027 — owner columns

**Files:**
- Create: `migrations/0027_import_jobs_owner.sql`
- Test: `tests/test_import_jobs_schema.py` (add one test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_import_jobs_schema.py`:

```python
def test_import_jobs_has_owner_columns(db_conn):
    aid = _archive_account(db_conn)
    jid = _insert_job(db_conn, aid)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs SET owner_host = 'h', owner_pid = 42 WHERE id = %s",
            (jid,),
        )
        cur.execute(
            "SELECT owner_host, owner_pid FROM import_jobs WHERE id = %s", (jid,)
        )
        row = cur.fetchone()
    assert row == ("h", 42)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_import_jobs_schema.py::test_import_jobs_has_owner_columns -v`
Expected: FAIL — `psycopg.errors.UndefinedColumn: column "owner_host" of relation "import_jobs" does not exist`

- [ ] **Step 3: Create the migration**

Create `migrations/0027_import_jobs_owner.sql`:

```sql
-- Ownership metadata for import_jobs (#162): record the host + pid of the
-- process that created (and runs) each import. Serve-startup reconcile then
-- reaps only genuinely orphaned jobs (a dead pid on this host, or a NULL
-- owner), leaving a live CLI import's row -- and the single-active busy-guard
-- -- intact. Nullable: any pre-existing active row has a NULL owner and is
-- treated as orphaned by reconcile.

ALTER TABLE import_jobs ADD COLUMN owner_host TEXT;
ALTER TABLE import_jobs ADD COLUMN owner_pid  INTEGER;
```

- [ ] **Step 4: Run test to verify it passes**

The `db_dsn` fixture calls `apply_migrations(TEST_DSN)` once per session, so the new migration applies automatically on the next run.

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_import_jobs_schema.py -v`
Expected: PASS (all four tests)

- [ ] **Step 5: Commit**

```bash
git add migrations/0027_import_jobs_owner.sql tests/test_import_jobs_schema.py
git commit -m "feat(imports): add owner_host/owner_pid to import_jobs (#162)"
```

---

## Task 2: Pure ownership module — `should_reap` + `pid_is_alive`

**Files:**
- Create: `src/localmail/importer/ownership.py`
- Test: `tests/test_importer_ownership.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_importer_ownership.py`:

```python
"""Unit tests for importer.ownership (pure orphan-detection helpers)."""
from __future__ import annotations

import os

from localmail.importer.ownership import pid_is_alive, should_reap

# Above any platform's pid_max (Linux default 4194304, macOS 99998), so this
# pid is never assigned to a live process.
_NEVER_ASSIGNED_PID = 2**31 - 1


def test_pid_is_alive_true_for_own_process():
    assert pid_is_alive(os.getpid()) is True


def test_pid_is_alive_false_for_never_assigned_pid():
    assert pid_is_alive(_NEVER_ASSIGNED_PID) is False


def test_should_reap_keeps_live_local_owner():
    assert should_reap(
        owner_host="host-a", owner_pid=123, current_host="host-a", pid_alive=True
    ) is False


def test_should_reap_reaps_dead_local_owner():
    assert should_reap(
        owner_host="host-a", owner_pid=123, current_host="host-a", pid_alive=False
    ) is True


def test_should_reap_reaps_null_owner_pid():
    assert should_reap(
        owner_host=None, owner_pid=None, current_host="host-a", pid_alive=False
    ) is True


def test_should_reap_keeps_foreign_host_even_if_pid_dead():
    assert should_reap(
        owner_host="host-b", owner_pid=123, current_host="host-a", pid_alive=False
    ) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_ownership.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.importer.ownership'`

- [ ] **Step 3: Write the module**

Create `src/localmail/importer/ownership.py`:

```python
"""Pure helpers for import-job ownership / orphan detection (#162).

Each import_jobs row records the host + pid of the process that created and runs
it. At serve startup, reconcile reaps an active row only when its owner is gone.
`should_reap` is the pure decision (no syscalls); `pid_is_alive` is the single
process-liveness syscall, isolated so `should_reap` stays unit-testable without
real pids.
"""
from __future__ import annotations

import os


def pid_is_alive(pid: int) -> bool:
    """True iff a process with `pid` currently exists.

    `os.kill(pid, 0)` sends no signal: it returns normally when the process is
    alive, raises ProcessLookupError when it is dead, and raises PermissionError
    when the process exists but is owned by another user (still alive).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def should_reap(
    *,
    owner_host: str | None,
    owner_pid: int | None,
    current_host: str,
    pid_alive: bool,
) -> bool:
    """Decide whether an active import_jobs row should be reaped as orphaned.

    Reap iff the owning process is gone:
      * ``owner_pid is None``          -> reap (legacy pre-0027 row, or a row
        that crashed between INSERT and commit -- unverifiable, treat as
        orphaned);
      * ``owner_host != current_host`` -> keep (another host's job; single-host
        is the project model, so we never reap what we cannot verify);
      * otherwise                      -> reap iff ``not pid_alive``.

    `pid_alive` is supplied by the caller (via `pid_is_alive`) so this predicate
    stays pure.
    """
    if owner_pid is None:
        return True
    if owner_host != current_host:
        return False
    return not pid_alive
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_ownership.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/importer/ownership.py tests/test_importer_ownership.py
git commit -m "feat(imports): pure should_reap + pid_is_alive helpers (#162)"
```

---

## Task 3: `create_job` records owner; `ImportJob` carries it

**Files:**
- Modify: `src/localmail/api/admin/imports.py` (dataclass, `_SELECT`, `create_job`)
- Test: `tests/test_api_admin_imports.py` (add one test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_admin_imports.py` (it already imports `svc`):

```python
def test_create_job_records_owner(db_conn):
    import os
    import socket

    aid = _account(db_conn, "arch")
    jid = svc.create_job(
        db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    db_conn.commit()
    job = svc.get_job(db_conn, jid)
    assert job.owner_host == socket.gethostname()
    assert job.owner_pid == os.getpid()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_imports.py::test_create_job_records_owner -v`
Expected: FAIL — `AttributeError: 'ImportJob' object has no attribute 'owner_host'`

- [ ] **Step 3: Add imports + dataclass fields + projection + INSERT**

In `src/localmail/api/admin/imports.py`:

3a. Add `os` and `socket` to the stdlib imports near the top (after `import threading`):

```python
import os
import socket
import threading
```

3b. Add two fields to the `ImportJob` dataclass, after `finished_at: datetime | None`:

```python
    finished_at: datetime | None
    owner_host: str | None
    owner_pid: int | None
```

3c. Extend `_SELECT` to project the two new columns (append them to the column list):

```python
_SELECT = """
    SELECT id, account_id, source_kind, source_path, status, total_messages,
           processed, inserted, skipped_dup, failed, error_msg, cancel_requested,
           last_progress_at, created_at, started_at, finished_at,
           owner_host, owner_pid
      FROM import_jobs
"""
```

3d. Replace the INSERT in `create_job` (the `cur.execute("INSERT INTO import_jobs ...")` inside the `try:`) with:

```python
        try:
            cur.execute(
                "INSERT INTO import_jobs "
                "  (account_id, source_kind, source_path, status, "
                "   owner_host, owner_pid) "
                "VALUES (%s, %s, %s, 'pending', %s, %s) RETURNING id",
                (account_id, source_kind, source_path,
                 socket.gethostname(), os.getpid()),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_imports.py -v`
Expected: PASS (existing tests still pass — `_SELECT` mapping is name-based via `class_row`, so the new columns flow into the new fields; `test_reconcile_orphaned_marks_active_failed` still passes here because the created owner pid is the live pytest process and the current default reconcile still reaps everything — that test is rewritten in Task 4).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/imports.py tests/test_api_admin_imports.py
git commit -m "feat(imports): record owner_host/owner_pid on create_job (#162)"
```

---

## Task 4: Selective `reconcile_orphaned_jobs`

**Files:**
- Modify: `src/localmail/api/admin/imports.py` (`reconcile_orphaned_jobs`, imports)
- Test: `tests/test_api_admin_imports.py` (rewrite one test, add four)

- [ ] **Step 1: Write the failing tests**

In `tests/test_api_admin_imports.py`, **replace** the existing `test_reconcile_orphaned_marks_active_failed` with the following block (rewrites the existing test for the new selective behaviour and adds the new cases):

```python
def _set_running(conn, jid):
    with conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET status='running' WHERE id=%s", (jid,))
    conn.commit()


def test_reconcile_reaps_dead_local_owner(db_conn):
    aid = _account(db_conn, "arch")
    jid = svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    _set_running(db_conn, jid)
    n = svc.reconcile_orphaned_jobs(db_conn, pid_alive=lambda _pid: False)
    db_conn.commit()
    assert n == 1
    job = svc.get_job(db_conn, jid)
    assert job.status == "failed"
    assert "interrupted" in (job.error_msg or "")


def test_reconcile_keeps_live_local_owner(db_conn):
    aid = _account(db_conn, "arch")
    jid = svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    _set_running(db_conn, jid)
    n = svc.reconcile_orphaned_jobs(db_conn, pid_alive=lambda _pid: True)
    db_conn.commit()
    assert n == 0
    assert svc.get_job(db_conn, jid).status == "running"


def test_reconcile_reaps_null_owner(db_conn):
    aid = _account(db_conn, "arch")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, 'mbox', '/a', 'running') RETURNING id",
            (aid,),
        )
        row = cur.fetchone()
    assert row is not None
    jid = int(row[0])
    db_conn.commit()
    n = svc.reconcile_orphaned_jobs(db_conn, pid_alive=lambda _pid: True)
    db_conn.commit()
    assert n == 1
    assert svc.get_job(db_conn, jid).status == "failed"


def test_reconcile_keeps_foreign_host(db_conn):
    aid = _account(db_conn, "arch")
    jid = svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    _set_running(db_conn, jid)
    n = svc.reconcile_orphaned_jobs(
        db_conn, current_host="some-other-host", pid_alive=lambda _pid: False)
    db_conn.commit()
    assert n == 0
    assert svc.get_job(db_conn, jid).status == "running"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_imports.py -k reconcile -v`
Expected: FAIL — `TypeError: reconcile_orphaned_jobs() got an unexpected keyword argument 'pid_alive'`

- [ ] **Step 3: Rewrite `reconcile_orphaned_jobs`**

3a. Add to the imports of `src/localmail/api/admin/imports.py`:

```python
from typing import Callable
```

and

```python
from localmail.importer.ownership import pid_is_alive, should_reap
```

(place the `ownership` import next to the existing `from localmail.importer.job_state import ACTIVE_STATUSES`).

3b. Replace the whole `reconcile_orphaned_jobs` function body with:

```python
def reconcile_orphaned_jobs(
    conn: psycopg.Connection,
    *,
    current_host: str | None = None,
    pid_alive: Callable[[int], bool] = pid_is_alive,
) -> int:
    """Mark genuinely-orphaned active jobs failed (called at serve startup).

    An active row is reaped only when its owning process is gone -- a dead pid
    on this host, or a NULL owner (legacy / never-started). A live import (e.g.
    a `localmail import` running in another process) keeps its row, so the
    single-active busy-guard stays held and no concurrent panel import can
    start. Returns the number of jobs reconciled. Caller commits.

    `current_host` / `pid_alive` are injectable for deterministic tests.
    """
    host = current_host if current_host is not None else socket.gethostname()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, owner_host, owner_pid FROM import_jobs WHERE status = ANY(%s)",
            (list(ACTIVE_STATUSES),),
        )
        reap_ids = [
            int(jid)
            for jid, owner_host, owner_pid in cur.fetchall()
            if should_reap(
                owner_host=owner_host,
                owner_pid=owner_pid,
                current_host=host,
                pid_alive=pid_alive(int(owner_pid)) if owner_pid is not None else False,
            )
        ]
        if not reap_ids:
            return 0
        cur.execute(
            "UPDATE import_jobs "
            "   SET status = 'failed', "
            "       error_msg = 'interrupted: serve process restarted', "
            "       finished_at = now() "
            " WHERE id = ANY(%s)",
            (reap_ids,),
        )
        return cur.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_imports.py -v`
Expected: PASS (all tests, including the four new reconcile cases)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/imports.py tests/test_api_admin_imports.py
git commit -m "feat(imports): reconcile only genuinely-orphaned jobs (#162)"
```

---

## Task 5: Docs + full-suite verification

**Files:**
- Modify: `CLAUDE.md` (schema-essentials table list + 2A.5 imports bullet)
- Check: `README.md` (update only if it documents reconcile/import-crash behaviour)

- [ ] **Step 1: Update CLAUDE.md schema essentials**

In the `## Schema essentials` "Tables:" sentence, `import_jobs` is already covered by the prose; add a one-line note where migration 0026/`import_jobs` ownership is relevant. Add this sentence to the 2A.5 imports bullet (the paragraph ending "**Migration `0026_import_jobs.sql`** (2A.5).") :

```markdown
  **Concurrent-CLI-safe reconcile (#162, resolved):** migration
  `0027_import_jobs_owner.sql` adds nullable `owner_host` / `owner_pid`,
  recorded at `create_job` time (the creating process is the running process
  for both the CLI and the in-serve thread). `reconcile_orphaned_jobs` now
  reaps an active row only when its owner is gone — a dead pid on this host
  (`importer/ownership.py::pid_is_alive`) or a NULL owner — via the pure
  predicate `ownership.should_reap`. A live `localmail import` in another
  process survives a serve restart, so the single-active busy-guard stays held
  and no concurrent panel import can start. `current_host` / `pid_alive` are
  injectable for tests. Accepted limitation: pid reuse can rarely keep a dead
  job's row until the next restart (self-heals; low probability on single-host).
```

Also update the "Migrations are tracked" / migration-latest references: in `## Conventions`, change "Latest is `0026_import_jobs.sql` (2A.5); next would be `0027_*.sql`." to "Latest is `0027_import_jobs_owner.sql` (#162); next would be `0028_*.sql`." and in `## Layout` update `migrations/` line to mention `0027_import_jobs_owner.sql`.

- [ ] **Step 2: Check README.md**

Run: `grep -n "reconcile\|import_jobs\|orphan" README.md`
If there are hits describing the reconcile/import-crash behaviour, update them to note that a live CLI import survives a serve restart. If there are no hits (likely — this is an internal robustness fix), make no README change.

- [ ] **Step 3: Full suite + mypy + ruff**

```bash
unset VIRTUAL_ENV && uv run pytest -q tests/ --deselect tests/test_daemon_control_socket.py
unset VIRTUAL_ENV && uv run mypy src/localmail
unset VIRTUAL_ENV && uv run ruff check src/localmail/importer/ownership.py \
    src/localmail/api/admin/imports.py tests/test_importer_ownership.py \
    tests/test_api_admin_imports.py tests/test_import_jobs_schema.py
```

Expected: pytest all pass (1439 + the new tests; the macOS-only `test_daemon_control_socket` AF_UNIX failures are excluded — they are a local-env issue); mypy clean; ruff clean.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs(imports): note concurrent-CLI-safe reconcile (#162)"
```

---

## Self-review notes (for the executor)

- **Spec coverage:** Task 1 = schema (§1); Task 3 = record owner at create (§2);
  Task 2 = pure `should_reap` + `pid_is_alive` (§3); Task 4 = selective reconcile
  (§3) + edge cases (§4: NULL owner, foreign host, live/dead pid); Task 5 = docs.
  All five spec sections are covered.
- **Type consistency:** `should_reap(*, owner_host, owner_pid, current_host,
  pid_alive)` and `pid_is_alive(pid)` are used identically in Tasks 2 and 4.
  `reconcile_orphaned_jobs(conn, *, current_host=None, pid_alive=pid_is_alive)`
  matches the test call sites in Task 4.
- **No new magic numbers** in source; the test's `_NEVER_ASSIGNED_PID` is a named
  constant with a justifying comment.
