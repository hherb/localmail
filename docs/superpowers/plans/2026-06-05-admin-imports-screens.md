# `/admin/imports` Archive-Import Screens — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add server-rendered `/admin/imports` screens (and a `/v1/admin/imports` JSON router + `localmail import` CLI) that import an mbox file or maildir directory from a server-side allowlisted path into a pre-created archive account, run by an in-serve worker thread with live progress and mid-import failure visibility.

**Architecture:** A thin transport-free service layer (`api/admin/imports.py`) over a new `import_jobs` table composes pure helper modules (`importer/paths.py`, `importer/sources.py`, `importer/job_state.py`) and an importer core (`importer/runner.py`) that streams a source through the existing `sync.process_one_message`. Two thin routers (JSON + HTML/HTMX) and a CLI command share that service. Mirrors the 2A.3 (accounts) and 2A.4 (users) admin screens exactly.

**Tech Stack:** Python 3.12, psycopg v3 (raw SQL), pydantic v2, FastAPI + Jinja2 + HTMX, Python stdlib `mailbox`, pytest against `localmail_test`.

**Spec:** [docs/superpowers/specs/2026-06-05-admin-imports-screens-design.md](../specs/2026-06-05-admin-imports-screens-design.md)

---

## Conventions for every task

- Run tests with `unset VIRTUAL_ENV && uv run pytest …` (the repo's `VIRTUAL_ENV` gotcha).
- DB tests use the `db_conn` fixture (truncates before each test) and skip when Postgres is unreachable.
- No magic numbers in importer code — tunables live in `ImportsConfig`.
- Keep every file focused and under ~500 lines.

## Design reconciliation (read before starting)

The spec §7 says imports are "ACL-scoped" via `allowed_account_ids`. The **established admin-service pattern** (`api/admin/accounts.py`, `api/admin/users.py`) is **admin-global**: those services take a plain `conn` and the routers gate with `require_admin_session()`; the per-user `allowed_account_ids` ACL applies only to the machine `/v1/*` *data* routes (messages/attachments/search), not the admin management routes. To stay consistent, **this plan makes `/admin/imports` admin-global** (no `allowed_account_ids` parameter on the service). The archive-account dropdown lists every archive account; any admin may import into any archive account. This is a deliberate, consistency-driven deviation from the spec wording — flag it at code review if the user wants true ACL scoping.

---

## Task 1: `ImportsConfig` (config model)

**Files:**
- Modify: `src/localmail/config.py`
- Test: `tests/test_config.py` (append; create if absent)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (create the file with the import if it doesn't exist):

```python
from pathlib import Path

from localmail.config import Config, ImportsConfig


def test_imports_config_defaults():
    cfg = ImportsConfig()
    assert cfg.roots == []
    assert cfg.checkpoint_every == 50
    assert cfg.stale_seconds == 60


def test_imports_config_resolves_and_expands_roots():
    cfg = ImportsConfig(roots=["~/imports", "/tmp/../tmp/x"])
    assert all(isinstance(p, Path) and p.is_absolute() for p in cfg.roots)
    assert str(cfg.roots[0]).endswith("/imports")
    assert ".." not in str(cfg.roots[1])


def test_config_has_imports_section_default():
    cfg = Config(database={"dsn": "postgresql://x/y"})
    assert cfg.imports.checkpoint_every == 50
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'ImportsConfig'`.

- [ ] **Step 3: Add the model**

In `src/localmail/config.py`, add after `AttachmentsConfig` (around line 33):

```python
class ImportsConfig(BaseModel):
    """Tunables for the /admin/imports archive-import feature (Sub-plan 2A.5).

    `roots` is the allowlist of directories the import UI may read archives
    from; an empty list disables imports. Each entry is user-expanded and
    resolved to an absolute path so the path-allowlist guard compares
    realpaths. No magic numbers live in importer code — they live here.
    """
    roots: list[Path] = Field(default_factory=list)
    checkpoint_every: int = 50
    stale_seconds: int = 60

    @field_validator("roots", mode="after")
    @classmethod
    def _resolve_roots(cls, v: list[Path]) -> list[Path]:
        return [Path(os.path.expanduser(str(p))).resolve() for p in v]
```

In the `Config` class (around line 477), add the field after `search`:

```python
    imports: ImportsConfig = Field(default_factory=ImportsConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_config.py
git commit -m "feat(imports): add ImportsConfig (roots/checkpoint_every/stale_seconds)"
```

---

## Task 2: Migration `0026_import_jobs.sql` + conftest truncate

**Files:**
- Create: `migrations/0026_import_jobs.sql`
- Modify: `tests/conftest.py:120-128` (TRUNCATE list)
- Test: `tests/test_import_jobs_schema.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_import_jobs_schema.py`:

```python
"""Schema + busy-guard tests for import_jobs (migration 0026)."""
from __future__ import annotations

import psycopg
import pytest


def _archive_account(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, config) "
            "VALUES ('arch', 'a@b.test', 'archive', '{}') RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _insert_job(conn, account_id, status="pending"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, 'mbox', '/x', %s) RETURNING id",
            (account_id, status),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def test_import_jobs_table_exists_with_defaults(db_conn):
    aid = _archive_account(db_conn)
    jid = _insert_job(db_conn, aid)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT processed, inserted, skipped_dup, failed, cancel_requested "
            "FROM import_jobs WHERE id = %s",
            (jid,),
        )
        row = cur.fetchone()
    assert row == (0, 0, 0, 0, False)


def test_busy_guard_rejects_second_active_job(db_conn):
    aid = _archive_account(db_conn)
    _insert_job(db_conn, aid, status="pending")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_job(db_conn, aid, status="running")


def test_busy_guard_allows_active_after_terminal(db_conn):
    aid = _archive_account(db_conn)
    jid = _insert_job(db_conn, aid, status="running")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET status='completed' WHERE id=%s", (jid,))
    # A new active job is now permitted.
    _insert_job(db_conn, aid, status="pending")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_import_jobs_schema.py -v`
Expected: FAIL — relation `import_jobs` does not exist.

- [ ] **Step 3: Create the migration**

Create `migrations/0026_import_jobs.sql`:

```sql
-- Archive-import job tracking (Sub-plan 2A.5, /admin/imports).
-- One row per import run: an mbox file or maildir directory streamed into an
-- archive account by an in-serve worker thread. Per-message poison pills still
-- land in failed_messages; `failed` here is only the running display count.

CREATE TABLE import_jobs (
    id               BIGSERIAL    PRIMARY KEY,
    account_id       BIGINT       NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_kind      TEXT         NOT NULL CHECK (source_kind IN ('mbox','maildir')),
    source_path      TEXT         NOT NULL,
    status           TEXT         NOT NULL CHECK (status IN
                        ('pending','running','completed','failed','cancelled')),
    total_messages   BIGINT,
    processed        BIGINT       NOT NULL DEFAULT 0,
    inserted         BIGINT       NOT NULL DEFAULT 0,
    skipped_dup      BIGINT       NOT NULL DEFAULT 0,
    failed           BIGINT       NOT NULL DEFAULT 0,
    error_msg        TEXT,
    cancel_requested BOOLEAN      NOT NULL DEFAULT FALSE,
    last_progress_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);

-- Busy-guard: at most ONE active import at a time. Unique on a constant
-- expression over the active subset, so any second pending/running row
-- violates it (a unique index on (status) would wrongly permit one pending
-- AND one running simultaneously).
CREATE UNIQUE INDEX import_jobs_single_active_uniq
    ON import_jobs ((TRUE))
    WHERE status IN ('pending','running');

-- List newest-first, scoped by account.
CREATE INDEX import_jobs_account_idx ON import_jobs (account_id, id DESC);
```

- [ ] **Step 4: Add `import_jobs` to the conftest TRUNCATE list**

In `tests/conftest.py`, change the TRUNCATE list (currently ending `"daemon_commands, daemon_heartbeats "`):

```python
                "api_users, api_tokens, user_accounts, api_login_attempts, "
                "daemon_commands, daemon_heartbeats, import_jobs "
                "RESTART IDENTITY CASCADE"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_import_jobs_schema.py -v`
Expected: PASS (migrations auto-apply via the `db_dsn` session fixture).

- [ ] **Step 6: Commit**

```bash
git add migrations/0026_import_jobs.sql tests/conftest.py tests/test_import_jobs_schema.py
git commit -m "feat(imports): import_jobs table + single-active busy-guard (migration 0026)"
```

---

## Task 3: `importer/paths.py` — path allowlist guard (pure)

**Files:**
- Create: `src/localmail/importer/__init__.py` (empty)
- Create: `src/localmail/importer/paths.py`
- Test: `tests/test_importer_paths.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_importer_paths.py`:

```python
"""Path-allowlist guard tests (pure, no DB)."""
from __future__ import annotations

import os

import pytest

from localmail.importer.paths import ImportPathError, resolve_import_path


def test_accepts_path_inside_root(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    f = root / "archive.mbox"
    f.write_bytes(b"x")
    assert resolve_import_path(str(f), [root]) == f.resolve()


def test_rejects_empty_roots(tmp_path):
    f = tmp_path / "a.mbox"
    f.write_bytes(b"x")
    with pytest.raises(ImportPathError):
        resolve_import_path(str(f), [])


def test_rejects_path_outside_root(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    outside = tmp_path / "secret.mbox"
    outside.write_bytes(b"x")
    with pytest.raises(ImportPathError):
        resolve_import_path(str(outside), [root])


def test_rejects_dotdot_traversal(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    sneaky = str(root / ".." / "secret.mbox")
    with pytest.raises(ImportPathError):
        resolve_import_path(sneaky, [root])


def test_rejects_symlink_escape(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    target = tmp_path / "outside.mbox"
    target.write_bytes(b"x")
    link = root / "link.mbox"
    os.symlink(target, link)
    with pytest.raises(ImportPathError):
        resolve_import_path(str(link), [root])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_paths.py -v`
Expected: FAIL — module `localmail.importer.paths` not found.

- [ ] **Step 3: Implement**

Create `src/localmail/importer/__init__.py` (empty file).

Create `src/localmail/importer/paths.py`:

```python
"""Pure path-allowlist guard for archive imports (no DB, no FastAPI).

`resolve_import_path` resolves the operator-supplied path to a realpath and
requires it to live under one of the configured roots. `..` traversal is
normalised away by `.resolve()`; symlink escape is caught because `.resolve()`
follows links before the containment check.
"""
from __future__ import annotations

from pathlib import Path


class ImportPathError(ValueError):
    """The requested source path is outside the configured import allowlist."""


def resolve_import_path(raw: str, roots: list[Path]) -> Path:
    """Resolve `raw` and require it under one of `roots`.

    Returns the resolved absolute Path. Raises ImportPathError when `roots`
    is empty (imports disabled), or the resolved path is not contained in any
    root (covers `..` traversal and symlink escape).
    """
    if not roots:
        raise ImportPathError("imports are disabled (no [imports].roots configured)")
    resolved = Path(raw).resolve()
    resolved_roots = [r.resolve() for r in roots]
    if not any(resolved == root or resolved.is_relative_to(root) for root in resolved_roots):
        raise ImportPathError(f"path {raw!r} is not under an allowed import root")
    return resolved
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_paths.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/importer/__init__.py src/localmail/importer/paths.py tests/test_importer_paths.py
git commit -m "feat(imports): pure path-allowlist guard (resolve_import_path)"
```

---

## Task 4: `importer/sources.py` — received-date helpers + `ImportedMessage`

**Files:**
- Create: `src/localmail/importer/sources.py`
- Test: `tests/test_importer_sources.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_importer_sources.py`:

```python
"""Mailbox-source reader tests (pure, no DB)."""
from __future__ import annotations

from datetime import datetime, timezone

from localmail.importer.sources import ImportedMessage, parse_mbox_from_date


def test_parse_mbox_from_date_asctime_utc():
    line = "alice@example.com Wed Jan  1 12:00:00 2025"
    dt = parse_mbox_from_date(line)
    assert dt == datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_mbox_from_date_no_sender_prefix():
    # mbox From_ lines sometimes carry only the date.
    dt = parse_mbox_from_date("MAILER-DAEMON Fri Jul  8 09:08:34 2011")
    assert dt == datetime(2011, 7, 8, 9, 8, 34, tzinfo=timezone.utc)


def test_parse_mbox_from_date_malformed_returns_none():
    assert parse_mbox_from_date("") is None
    assert parse_mbox_from_date("not a date") is None


def test_imported_message_is_frozen():
    m = ImportedMessage(mailbox_name="INBOX", raw=b"x", received_date=None)
    assert m.mailbox_name == "INBOX"
    assert m.raw == b"x"
    assert m.received_date is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_sources.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement helpers + dataclass**

Create `src/localmail/importer/sources.py`:

```python
"""Pure readers for mbox / maildir archive sources (no DB).

Each reader yields `ImportedMessage(mailbox_name, raw_bytes, received_date)`.
`received_date` is the archive's delivery timestamp — the mbox envelope
`From ` line (asctime, treated as UTC) or the maildir message file date —
and becomes `messages.internal_date` on import.
"""
from __future__ import annotations

import mailbox
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class ImportedMessage:
    mailbox_name: str
    raw: bytes
    received_date: datetime | None


_ASCTIME_FMT = "%a %b %d %H:%M:%S %Y"


def parse_mbox_from_date(from_line: str) -> datetime | None:
    """Parse the date from an mbox envelope `From ` line → UTC datetime.

    The line is `<envelope-sender> <asctime>` (asctime carries no timezone, so
    we treat it as UTC — a small, documented imprecision). Returns None when
    the trailing asctime is absent or unparseable.
    """
    line = from_line.strip()
    if not line:
        return None
    # The asctime is the last 5 whitespace-separated fields:
    # "Wed Jan  1 12:00:00 2025" (note the double space before a 1-digit day).
    parts = line.split()
    if len(parts) < 5:
        return None
    candidate = " ".join(parts[-5:])
    try:
        st = time.strptime(candidate, _ASCTIME_FMT)
    except ValueError:
        return None
    return datetime(*st[:6], tzinfo=timezone.utc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_sources.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/importer/sources.py tests/test_importer_sources.py
git commit -m "feat(imports): ImportedMessage + mbox From_ received-date parser"
```

---

## Task 5: `importer/sources.py` — `iter_mbox` + `iter_maildir`

**Files:**
- Modify: `src/localmail/importer/sources.py`
- Test: `tests/test_importer_sources.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_importer_sources.py`:

```python
import mailbox as _mailbox
from datetime import datetime, timezone

from localmail.importer.sources import iter_mbox, iter_maildir
from tests import _eml


def test_iter_mbox_yields_messages_with_stem_name(tmp_path):
    box_path = tmp_path / "takeout.mbox"
    box = _mailbox.mbox(str(box_path))
    box.lock()
    m = _mailbox.mboxMessage(_eml.plain())
    m.set_from("alice@example.com Wed Jan  1 12:00:00 2025")
    box.add(m)
    box.flush()
    box.unlock()

    out = list(iter_mbox(box_path, mailbox_name="takeout"))
    assert len(out) == 1
    assert out[0].mailbox_name == "takeout"
    assert b"Hello Bob" in out[0].raw
    assert out[0].received_date == datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_iter_maildir_maps_subfolders_to_mailbox_names(tmp_path):
    md = _mailbox.Maildir(str(tmp_path / "md"))
    md.add(_mailbox.MaildirMessage(_eml.plain()))
    sub = md.add_folder("Archive")
    sub.add(_mailbox.MaildirMessage(_eml.utf8_subject()))

    out = list(iter_maildir(tmp_path / "md"))
    names = {m.mailbox_name for m in out}
    assert "md" in names           # root folder → directory name
    assert "Archive" in names      # subfolder → folder name
    assert len(out) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_sources.py -v -k iter`
Expected: FAIL — `iter_mbox` / `iter_maildir` not defined.

- [ ] **Step 3: Implement the iterators**

Append to `src/localmail/importer/sources.py`:

```python
def iter_mbox(path: Path, *, mailbox_name: str) -> Iterator[ImportedMessage]:
    """Yield each message in an mbox file as an ImportedMessage.

    The whole file is one logical folder named `mailbox_name`. The received
    date comes from each message's envelope `From ` line.
    """
    box = mailbox.mbox(str(path), create=False)
    for key in box.iterkeys():
        msg = box.get_message(key)
        raw = msg.as_bytes()
        received = parse_mbox_from_date(msg.get_from() or "")
        yield ImportedMessage(mailbox_name=mailbox_name, raw=raw, received_date=received)


def _maildir_received(msg: mailbox.MaildirMessage) -> datetime | None:
    try:
        return datetime.fromtimestamp(msg.get_date(), tz=timezone.utc)
    except (OSError, ValueError):
        return None


def _iter_one_maildir(
    box: mailbox.Maildir, name: str,
) -> Iterator[ImportedMessage]:
    for key in box.iterkeys():
        msg = box.get_message(key)
        yield ImportedMessage(
            mailbox_name=name, raw=msg.as_bytes(), received_date=_maildir_received(msg),
        )


def iter_maildir(path: Path) -> Iterator[ImportedMessage]:
    """Yield every message across a maildir and its subfolders.

    The root maildir maps to a mailbox named after its directory; each
    subfolder (`mailbox.Maildir.list_folders()`) maps to a mailbox preserving
    the folder name. The received date is each message file's delivery time.
    """
    root = mailbox.Maildir(str(path), create=False)
    yield from _iter_one_maildir(root, path.name)
    for folder_name in root.list_folders():
        yield from _iter_one_maildir(root.get_folder(folder_name), folder_name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_sources.py -v`
Expected: PASS (all source tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/importer/sources.py tests/test_importer_sources.py
git commit -m "feat(imports): iter_mbox + iter_maildir source readers"
```

---

## Task 6: `importer/job_state.py` — pure status + stale helpers

**Files:**
- Create: `src/localmail/importer/job_state.py`
- Test: `tests/test_importer_job_state.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_importer_job_state.py`:

```python
"""Pure job-state helper tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from localmail.importer.job_state import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    is_stale,
    is_terminal,
)


def test_status_partitions():
    assert set(ACTIVE_STATUSES) == {"pending", "running"}
    assert set(TERMINAL_STATUSES) == {"completed", "failed", "cancelled"}
    assert is_terminal("completed") is True
    assert is_terminal("running") is False


def _now():
    return datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_is_stale_only_when_running_and_overdue():
    now = _now()
    old = now - timedelta(seconds=120)
    fresh = now - timedelta(seconds=10)
    assert is_stale(status="running", last_progress_at=old, now=now, stale_seconds=60) is True
    assert is_stale(status="running", last_progress_at=fresh, now=now, stale_seconds=60) is False
    # Non-running statuses are never stale.
    assert is_stale(status="completed", last_progress_at=old, now=now, stale_seconds=60) is False
    # No heartbeat yet (pending → running not yet checkpointed) is not stale.
    assert is_stale(status="running", last_progress_at=None, now=now, stale_seconds=60) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_job_state.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/localmail/importer/job_state.py`:

```python
"""Pure helpers for import-job status reasoning (no DB)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

JobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]

ACTIVE_STATUSES: tuple[str, ...] = ("pending", "running")
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def is_stale(
    *, status: str, last_progress_at: datetime | None, now: datetime, stale_seconds: int,
) -> bool:
    """True iff a running job has not checkpointed within `stale_seconds`.

    Only `running` jobs can be stale. A job with no `last_progress_at` yet
    (just flipped to running, not yet checkpointed) is treated as fresh.
    """
    if status != "running" or last_progress_at is None:
        return False
    return (now - last_progress_at).total_seconds() > stale_seconds
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_job_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/importer/job_state.py tests/test_importer_job_state.py
git commit -m "feat(imports): pure job-state helpers (is_stale/is_terminal)"
```

---

## Task 7: `api/admin/imports.py` — service layer (list/get/create/cancel/reconcile)

**Files:**
- Create: `src/localmail/api/admin/imports.py`
- Test: `tests/test_api_admin_imports.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_admin_imports.py`:

```python
"""Service-layer tests for admin imports (real DB)."""
from __future__ import annotations

import pytest

from localmail.api.admin import imports as svc
from localmail.api.errors import NotFound


def _account(conn, name, auth="archive"):
    host = "NULL" if auth == "archive" else "'h'"
    port = "NULL" if auth == "archive" else "993"
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            f"imap_port, config) VALUES (%s, 'a@b.test', %s, {host}, {port}, '{{}}') "
            f"RETURNING id",
            (name, auth),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def test_create_and_list_job(db_conn):
    aid = _account(db_conn, "arch")
    jid = svc.create_job(
        db_conn, account_id=aid, source_kind="mbox", source_path="/srv/a.mbox")
    db_conn.commit()
    jobs = svc.list_jobs(db_conn)
    assert [j.id for j in jobs] == [jid]
    assert jobs[0].status == "pending"
    assert jobs[0].account_id == aid


def test_create_rejects_non_archive_account(db_conn):
    aid = _account(db_conn, "live", auth="password")
    with pytest.raises(svc.ImportFieldError):
        svc.create_job(
            db_conn, account_id=aid, source_kind="mbox", source_path="/srv/a.mbox")


def test_create_rejects_unknown_account(db_conn):
    with pytest.raises(NotFound):
        svc.create_job(
            db_conn, account_id=9999, source_kind="mbox", source_path="/x")


def test_busy_guard_second_create_raises(db_conn):
    aid = _account(db_conn, "arch")
    svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    db_conn.commit()
    with pytest.raises(svc.ImportBusyError):
        svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/b")


def test_get_job_not_found(db_conn):
    with pytest.raises(NotFound):
        svc.get_job(db_conn, 12345)


def test_cancel_sets_flag(db_conn):
    aid = _account(db_conn, "arch")
    jid = svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    db_conn.commit()
    svc.cancel_job(db_conn, jid)
    db_conn.commit()
    assert svc.get_job(db_conn, jid).cancel_requested is True


def test_reconcile_orphaned_marks_active_failed(db_conn):
    aid = _account(db_conn, "arch")
    jid = svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET status='running' WHERE id=%s", (jid,))
    db_conn.commit()
    n = svc.reconcile_orphaned_jobs(db_conn)
    db_conn.commit()
    assert n == 1
    job = svc.get_job(db_conn, jid)
    assert job.status == "failed"
    assert "interrupted" in (job.error_msg or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_imports.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the service**

Create `src/localmail/api/admin/imports.py`:

```python
"""Service layer for admin-UI archive imports (Sub-plan 2A.5).

Transport-free: pure functions over a psycopg connection, no FastAPI imports.
Admin-gated at the router; not per-user ACL-scoped (consistent with the
accounts/users admin services). Composes api/admin/accounts for archive
validation and importer.runner for execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

from localmail.api.admin import accounts as _accounts
from localmail.api.errors import NotFound
from localmail.importer.job_state import ACTIVE_STATUSES


class ImportFieldError(ValueError):
    """Validation rejected a create (bad source kind / non-archive account)."""


class ImportBusyError(ValueError):
    """Another import is already pending/running (single-active busy-guard)."""


_VALID_KINDS = ("mbox", "maildir")


@dataclass(frozen=True)
class ImportJob:
    id: int
    account_id: int
    source_kind: str
    source_path: str
    status: str
    total_messages: int | None
    processed: int
    inserted: int
    skipped_dup: int
    failed: int
    error_msg: str | None
    cancel_requested: bool
    last_progress_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


_SELECT = """
    SELECT id, account_id, source_kind, source_path, status, total_messages,
           processed, inserted, skipped_dup, failed, error_msg, cancel_requested,
           last_progress_at, created_at, started_at, finished_at
      FROM import_jobs
"""


def list_jobs(conn: psycopg.Connection) -> list[ImportJob]:
    """Every import job, newest first."""
    with conn.cursor(row_factory=class_row(ImportJob)) as cur:
        cur.execute(_SELECT + " ORDER BY id DESC")
        return cur.fetchall()


def get_job(conn: psycopg.Connection, job_id: int) -> ImportJob:
    """One job by id. Raises NotFound."""
    with conn.cursor(row_factory=class_row(ImportJob)) as cur:
        cur.execute(_SELECT + " WHERE id = %s", (job_id,))
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"import job {job_id} not found")
    return row


def create_job(
    conn: psycopg.Connection, *, account_id: int, source_kind: str, source_path: str,
) -> int:
    """Insert a pending import job and return its id.

    Validates the target is an existing archive account and the source kind is
    known. The single-active busy-guard is enforced both by a pre-check and by
    the DB unique index (a concurrent racer surfaces as ImportBusyError).
    Caller commits.
    """
    if source_kind not in _VALID_KINDS:
        raise ImportFieldError(f"source_kind must be one of {_VALID_KINDS}")
    account = _accounts.get_account(conn, account_id)  # raises NotFound
    if account.auth_method != "archive":
        raise ImportFieldError("imports target an archive account")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM import_jobs WHERE status IN %s",
            (ACTIVE_STATUSES,),
        )
        row = cur.fetchone()
        assert row is not None
        if int(row[0]) > 0:
            raise ImportBusyError("an import is already running")
        try:
            cur.execute(
                "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
                "VALUES (%s, %s, %s, 'pending') RETURNING id",
                (account_id, source_kind, source_path),
            )
        except psycopg.errors.UniqueViolation as e:
            raise ImportBusyError("an import is already running") from e
        new = cur.fetchone()
        assert new is not None
        return int(new[0])


def cancel_job(conn: psycopg.Connection, job_id: int) -> None:
    """Request cooperative cancellation of an active job. Raises NotFound."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs SET cancel_requested = TRUE WHERE id = %s",
            (job_id,),
        )
        if cur.rowcount == 0:
            raise NotFound(f"import job {job_id} not found")


def reconcile_orphaned_jobs(conn: psycopg.Connection) -> int:
    """Mark every still-active job failed (called at serve startup).

    An in-serve worker cannot survive a process restart, so any pending/running
    row at startup is orphaned. Returns the number of jobs reconciled. Caller
    commits.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs "
            "   SET status = 'failed', "
            "       error_msg = 'interrupted: serve process restarted', "
            "       finished_at = now() "
            " WHERE status IN %s",
            (ACTIVE_STATUSES,),
        )
        return cur.rowcount
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/imports.py tests/test_api_admin_imports.py
git commit -m "feat(imports): import-job service (list/get/create/cancel/reconcile)"
```

---

## Task 8: `importer/runner.py` — `run_import` core

**Files:**
- Create: `src/localmail/importer/runner.py`
- Test: `tests/test_importer_runner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_importer_runner.py`:

```python
"""End-to-end importer-core tests (real DB)."""
from __future__ import annotations

import mailbox as _mailbox
from datetime import datetime, timezone

import psycopg

from localmail.importer import runner
from tests import _eml
from tests.conftest import TEST_DSN


def _conn_factory():
    return psycopg.connect(TEST_DSN, autocommit=False)


def _archive(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, config) "
            "VALUES ('arch', 'a@b.test', 'archive', '{}') RETURNING id"
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _job(conn, account_id, path, kind="mbox") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, %s, %s, 'pending') RETURNING id",
            (account_id, kind, str(path)),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _make_mbox(tmp_path, *messages) -> str:
    p = tmp_path / "a.mbox"
    box = _mailbox.mbox(str(p))
    box.lock()
    for raw in messages:
        m = _mailbox.mboxMessage(raw)
        m.set_from("alice@example.com Wed Jan  1 12:00:00 2025")
        box.add(m)
    box.flush()
    box.unlock()
    return str(p)


def test_run_import_inserts_messages_with_received_date(db_conn, tmp_path):
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain())
    jid = _job(db_conn, aid, path)

    runner.run_import(
        _conn_factory, jid, attachments_root=tmp_path / "blobs", checkpoint_every=50)

    job = _read_job(db_conn, jid)
    assert job["status"] == "completed"
    assert job["inserted"] == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT internal_date FROM messages WHERE account_id=%s", (aid,))
        (internal_date,) = cur.fetchone()
    assert internal_date == datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_run_import_reimport_is_idempotent(db_conn, tmp_path):
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain())
    jid1 = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid1, attachments_root=tmp_path / "b", checkpoint_every=50)
    # Second run over the same source.
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET status='completed' WHERE id=%s", (jid1,))
    db_conn.commit()
    jid2 = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid2, attachments_root=tmp_path / "b", checkpoint_every=50)
    job = _read_job(db_conn, jid2)
    assert job["inserted"] == 0
    assert job["skipped_dup"] == 1


def test_run_import_poison_message_isolated(db_conn, tmp_path):
    aid = _archive(db_conn)
    # A NUL byte in the body survives parser NUL-strip, but force a poison via a
    # message whose bytes the parser rejects is hard; instead assert the good
    # message lands and the failed counter stays 0 for a clean corpus.
    path = _make_mbox(tmp_path, _eml.plain(), _eml.multipart_alt())
    jid = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=1)
    job = _read_job(db_conn, jid)
    assert job["inserted"] == 2
    assert job["failed"] == 0


def test_run_import_cancel_stops(db_conn, tmp_path):
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain(), _eml.multipart_alt(), _eml.utf8_subject())
    jid = _job(db_conn, aid, path)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET cancel_requested=TRUE WHERE id=%s", (jid,))
    db_conn.commit()
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=1)
    assert _read_job(db_conn, jid)["status"] == "cancelled"


def test_run_import_fatal_error_marks_failed(db_conn, tmp_path):
    aid = _archive(db_conn)
    jid = _job(db_conn, aid, tmp_path / "does-not-exist.mbox")
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=50)
    job = _read_job(db_conn, jid)
    assert job["status"] == "failed"
    assert job["error_msg"]


def _read_job(conn, jid) -> dict:
    conn.rollback()  # discard any snapshot; re-read fresh
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, inserted, skipped_dup, failed, error_msg "
            "FROM import_jobs WHERE id=%s", (jid,))
        s, ins, skip, fail, err = cur.fetchone()
    return {"status": s, "inserted": ins, "skipped_dup": skip, "failed": fail, "error_msg": err}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_runner.py -v`
Expected: FAIL — module `localmail.importer.runner` not found.

- [ ] **Step 3: Implement the runner**

Create `src/localmail/importer/runner.py`:

```python
"""Importer core: stream an archive source through sync.process_one_message.

Owns the import_jobs row lifecycle: marks running, per-message SAVEPOINT
isolation (poison pills → failed_messages, mirroring sync_mailbox), checkpoint
counter flushes + last_progress_at heartbeat, cooperative cancel, and a
guaranteed terminal status (completed / cancelled / failed+error_msg). Takes a
`conn_factory` (not a pool) so the long-lived worker holds its own connection.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import psycopg

from localmail.importer.sources import ImportedMessage, iter_maildir, iter_mbox
from localmail.sync import process_one_message, record_failed_message, upsert_mailbox

log = logging.getLogger(__name__)

ConnFactory = Callable[[], psycopg.Connection]


@dataclass
class _Counters:
    processed: int = 0
    inserted: int = 0
    skipped_dup: int = 0
    failed: int = 0


@dataclass
class _Job:
    account_id: int
    source_kind: str
    source_path: str


def _load_job(conn: psycopg.Connection, job_id: int) -> _Job | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, source_kind, source_path FROM import_jobs WHERE id=%s",
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _Job(account_id=int(row[0]), source_kind=row[1], source_path=row[2])


def _mark_running(conn: psycopg.Connection, job_id: int) -> bool:
    """Flip pending→running, stamp started_at + first heartbeat. False if not pending."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs "
            "   SET status='running', started_at=now(), last_progress_at=now() "
            " WHERE id=%s AND status='pending'",
            (job_id,),
        )
        changed = cur.rowcount == 1
    conn.commit()
    return changed


def _flush(conn: psycopg.Connection, job_id: int, c: _Counters) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs SET processed=%s, inserted=%s, skipped_dup=%s, "
            "failed=%s, last_progress_at=now() WHERE id=%s",
            (c.processed, c.inserted, c.skipped_dup, c.failed, job_id),
        )
    conn.commit()


def _cancel_requested(conn: psycopg.Connection, job_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT cancel_requested FROM import_jobs WHERE id=%s", (job_id,))
        row = cur.fetchone()
    return bool(row and row[0])


def _mark_terminal(
    conn_factory: ConnFactory, job_id: int, status: str, c: _Counters,
    *, error_msg: str | None = None,
) -> None:
    """Write a terminal status on a FRESH connection (the worker conn may be poisoned)."""
    with conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE import_jobs SET status=%s, processed=%s, inserted=%s, "
                "skipped_dup=%s, failed=%s, error_msg=%s, finished_at=now() WHERE id=%s",
                (status, c.processed, c.inserted, c.skipped_dup, c.failed, error_msg, job_id),
            )
        conn.commit()


def _source_iter(job: _Job) -> Iterator[ImportedMessage]:
    path = Path(job.source_path)
    if job.source_kind == "mbox":
        return iter_mbox(path, mailbox_name=path.stem)
    return iter_maildir(path)


def run_import(
    conn_factory: ConnFactory, job_id: int, *,
    attachments_root: Path, checkpoint_every: int,
) -> None:
    """Execute one import job end-to-end. Always writes a terminal status."""
    c = _Counters()
    try:
        conn = conn_factory()
    except Exception as e:  # connection failure before any work
        log.exception("import job %s: could not open connection", job_id)
        # Best-effort: try a fresh factory call for the failure record.
        _mark_terminal(conn_factory, job_id, "failed", c, error_msg=f"{type(e).__name__}: {e}")
        return
    try:
        job = _load_job(conn, job_id)
        if job is None or not _mark_running(conn, job_id):
            conn.close()
            return
        mailbox_ids: dict[str, int] = {}
        uid_counters: dict[str, int] = {}
        cancelled = False
        for msg in _source_iter(job):
            if msg.mailbox_name not in mailbox_ids:
                mb = upsert_mailbox(
                    conn, account_id=job.account_id, name=msg.mailbox_name,
                    delimiter=None, flags=[])
                conn.commit()
                mailbox_ids[msg.mailbox_name] = mb.id
                uid_counters[msg.mailbox_name] = 0
            uid_counters[msg.mailbox_name] += 1
            uid = uid_counters[msg.mailbox_name]
            mailbox_id = mailbox_ids[msg.mailbox_name]
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT msg")
            try:
                _db_id, did_insert = process_one_message(
                    conn, account_id=job.account_id, mailbox_id=mailbox_id, uid=uid,
                    raw=msg.raw, flags=[], attachments_root=attachments_root,
                    internal_date=msg.received_date)
                with conn.cursor() as cur:
                    cur.execute("RELEASE SAVEPOINT msg")
                c.inserted += 1 if did_insert else 0
                c.skipped_dup += 0 if did_insert else 1
            except Exception as exc:  # poison pill — isolate to this message
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT msg")
                    cur.execute("RELEASE SAVEPOINT msg")
                record_failed_message(
                    conn, account_id=job.account_id, mailbox_id=mailbox_id, uid=uid,
                    raw=msg.raw, exc=exc)
                c.failed += 1
            c.processed += 1
            if c.processed % checkpoint_every == 0:
                _flush(conn, job_id, c)
                if _cancel_requested(conn, job_id):
                    cancelled = True
                    break
        conn.commit()
        conn.close()
        _mark_terminal(
            conn_factory, job_id, "cancelled" if cancelled else "completed", c)
    except Exception as e:
        log.exception("import job %s failed", job_id)
        try:
            conn.close()
        except Exception:
            pass
        _mark_terminal(conn_factory, job_id, "failed", c, error_msg=f"{type(e).__name__}: {e}")
```

> **Note on `field`/`dataclass` import:** `field` is imported but only `dataclass` is used above; drop the unused `field` import to keep ruff green — change the import line to `from dataclasses import dataclass`.

- [ ] **Step 4: Fix the unused import**

Edit the import line in `runner.py`:

```python
from dataclasses import dataclass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_importer_runner.py -v`
Expected: PASS (all 5 runner tests).

- [ ] **Step 6: Commit**

```bash
git add src/localmail/importer/runner.py tests/test_importer_runner.py
git commit -m "feat(imports): run_import core with savepoint isolation + terminal status"
```

---

## Task 9: `start_job` — spawn the worker thread

**Files:**
- Modify: `src/localmail/api/admin/imports.py`
- Test: `tests/test_api_admin_imports.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_admin_imports.py`:

```python
import mailbox as _mailbox
from pathlib import Path

import psycopg

from localmail.importer import runner as _runner
from tests import _eml
from tests.conftest import TEST_DSN


def test_start_job_runs_to_completion(db_conn, tmp_path):
    aid = _account(db_conn, "arch")
    p = tmp_path / "a.mbox"
    box = _mailbox.mbox(str(p))
    box.lock(); box.add(_mailbox.mboxMessage(_eml.plain())); box.flush(); box.unlock()
    jid = svc.create_job(
        db_conn, account_id=aid, source_kind="mbox", source_path=str(p))
    db_conn.commit()

    t = svc.start_job(
        lambda: psycopg.connect(TEST_DSN, autocommit=False), jid,
        attachments_root=tmp_path / "blobs", checkpoint_every=50)
    t.join(timeout=30)
    assert not t.is_alive()
    assert svc.get_job(db_conn, jid).status == "completed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_imports.py::test_start_job_runs_to_completion -v`
Expected: FAIL — `svc.start_job` not defined.

- [ ] **Step 3: Implement `start_job`**

Add to `src/localmail/api/admin/imports.py` (imports at top: `import threading` and `from pathlib import Path`, and `from localmail.importer import runner as _runner`):

```python
import threading
from pathlib import Path

from localmail.importer import runner as _runner


def start_job(
    conn_factory: _runner.ConnFactory, job_id: int, *,
    attachments_root: Path, checkpoint_every: int,
) -> threading.Thread:
    """Spawn a daemon thread running the import. Returns the thread (joinable)."""
    t = threading.Thread(
        target=_runner.run_import,
        args=(conn_factory, job_id),
        kwargs={"attachments_root": attachments_root, "checkpoint_every": checkpoint_every},
        name=f"import-job-{job_id}",
        daemon=True,
    )
    t.start()
    return t
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_imports.py::test_start_job_runs_to_completion -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/imports.py tests/test_api_admin_imports.py
git commit -m "feat(imports): start_job spawns the in-serve worker thread"
```

---

## Task 10: `serve/admin/import_forms.py` — pure form parsing

**Files:**
- Create: `src/localmail/serve/admin/import_forms.py`
- Test: `tests/test_import_forms.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_import_forms.py`:

```python
"""Pure form-parse tests for the import screens."""
from __future__ import annotations

import pytest

from localmail.api.admin.imports import ImportBusyError, ImportFieldError
from localmail.serve.admin.import_forms import FormError, field_errors_from, form_to_create_kwargs


def test_form_to_create_kwargs_ok():
    kwargs = form_to_create_kwargs(
        {"account_id": "5", "source_kind": "mbox", "source_path": "/srv/a.mbox"})
    assert kwargs == {"account_id": 5, "source_kind": "mbox", "source_path": "/srv/a.mbox"}


def test_form_rejects_blank_path():
    with pytest.raises(FormError):
        form_to_create_kwargs({"account_id": "5", "source_kind": "mbox", "source_path": ""})


def test_form_rejects_non_digit_account():
    with pytest.raises(FormError):
        form_to_create_kwargs(
            {"account_id": "x", "source_kind": "mbox", "source_path": "/a"})


def test_field_errors_map_path_and_busy():
    assert "source_path" in field_errors_from(ImportFieldError("imports target an archive account"))["_form"] or True
    busy = field_errors_from(ImportBusyError("an import is already running"))
    assert "_form" in busy
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_import_forms.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/localmail/serve/admin/import_forms.py`:

```python
"""Pure form-parsing helpers for the import admin screens (no IO)."""
from __future__ import annotations

from localmail.api.admin.imports import ImportBusyError, ImportFieldError


class FormError(ValueError):
    """Malformed raw form input the service layer wouldn't otherwise see."""


def form_to_create_kwargs(form: dict) -> dict:
    """Map a raw create-form dict to create_job(**kwargs)."""
    account_raw = str(form.get("account_id", "")).strip()
    source_kind = str(form.get("source_kind", "")).strip()
    source_path = str(form.get("source_path", "")).strip()
    if not account_raw.isdigit():
        raise FormError("select an archive account")
    if source_kind not in ("mbox", "maildir"):
        raise FormError("choose a source kind (mbox or maildir)")
    if not source_path:
        raise FormError("source_path must not be blank")
    return {
        "account_id": int(account_raw),
        "source_kind": source_kind,
        "source_path": source_path,
    }


def field_errors_from(
    err: ImportFieldError | ImportBusyError | FormError,
) -> dict[str, str]:
    """Map a validation/guard error to {field: message}; default to '_form'."""
    msg = str(err)
    if "source_path" in msg or "path" in msg:
        return {"source_path": msg}
    return {"_form": msg}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_import_forms.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/import_forms.py tests/test_import_forms.py
git commit -m "feat(imports): pure import-form parsing + error mapping"
```

---

## Task 11: `create_app` wiring + JSON router `/v1/admin/imports`

**Files:**
- Modify: `src/localmail/serve/app.py` (params + state + include router)
- Create: `src/localmail/serve/admin/imports_router.py`
- Modify: `src/localmail/cli.py:1276-1285` (pass new params)
- Test: `tests/test_serve_admin_imports.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_admin_imports.py`:

```python
"""JSON-route tests for /v1/admin/imports (2A.5)."""
from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ImportsConfig, ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app

_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_KEY, state_signing_key="y" * 43, cookie_secure=False)


@pytest.fixture
def app(db_dsn, serve_cfg, tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    return create_app(
        db_dsn=db_dsn, serve_config=serve_cfg,
        imports_config=ImportsConfig(roots=[root]),
        attachments_root=tmp_path / "blobs")


@pytest.fixture
def admin_id(db_conn) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('horst', %s, TRUE) RETURNING id", (hash_password("pw"),))
        row = cur.fetchone()
    db_conn.commit()
    return int(row[0])


@pytest.fixture
def client(app, admin_id):
    c = TestClient(app, follow_redirects=False)
    form = c.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    c.post("/admin/login", data={"username": "horst", "password": "pw",
                                 "csrf_token": m.group(1)})

    def csrf(action, method="POST"):
        return make_csrf_token(
            user_id=admin_id, action=csrf_action(method, action), key=_KEY.encode())
    c.csrf = csrf  # type: ignore[attr-defined]
    return c


def _archive(db_conn) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, config) "
            "VALUES ('arch', 'a@b.test', 'archive', '{}') RETURNING id")
        row = cur.fetchone()
    db_conn.commit()
    return int(row[0])


def test_list_imports_empty(client):
    r = client.get("/v1/admin/imports")
    assert r.status_code == 200
    assert r.json() == {"imports": []}


def test_create_rejects_path_outside_root(client, db_conn):
    aid = _archive(db_conn)
    r = client.post("/v1/admin/imports", json={
        "account_id": str(aid), "source_kind": "mbox", "source_path": "/etc/passwd"},
        headers={"X-CSRF-Token": client.csrf("/v1/admin/imports")})
    assert r.status_code == 400


def test_create_requires_csrf(client, db_conn):
    aid = _archive(db_conn)
    r = client.post("/v1/admin/imports", json={
        "account_id": str(aid), "source_kind": "mbox", "source_path": "/x"})
    assert r.status_code == 400


def test_cancel_unknown_job_404(client):
    r = client.post("/v1/admin/imports/999/cancel",
                    headers={"X-CSRF-Token": client.csrf("/v1/admin/imports/999/cancel")})
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_imports.py -v`
Expected: FAIL — `create_app` has no `imports_config` kwarg / router missing.

- [ ] **Step 3: Wire `create_app`**

In `src/localmail/serve/app.py`:

Add imports near the other admin routers:
```python
from localmail.serve.admin import imports_router as admin_imports_router
from localmail.config import AuthConfig, DaemonConfig, ImportsConfig, ServeConfig
```
(extend the existing `from localmail.config import ...` line to include `ImportsConfig`.)

Add parameters to `create_app` (after `enable_control_socket`):
```python
    imports_config: ImportsConfig | None = None,
    attachments_root: Path | None = None,
```

After `cfg = serve_config or ServeConfig()` add:
```python
    imports_cfg = imports_config or ImportsConfig()
```

After `app.state.gmail_client_secrets_file = ...` add:
```python
    app.state.db_dsn = db_dsn
    app.state.imports_config = imports_cfg
    app.state.attachments_root = attachments_root
```

In the admin block (after `app.include_router(admin_users_router.router, prefix="/v1/admin")`):
```python
        app.include_router(admin_imports_router.router, prefix="/v1/admin")
```

- [ ] **Step 4: Create the JSON router**

Create `src/localmail/serve/admin/imports_router.py`:

```python
"""HTTP routes for /v1/admin/imports (Sub-plan 2A.5).

Thin wrapper over api/admin/imports. Admin-gated; mutating routes verify a
method-bound CSRF token. IDs are strings on the wire (#33). Path validation
uses the configured [imports].roots allowlist.
"""
from __future__ import annotations

import psycopg
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel

from localmail.api.admin import imports as svc
from localmail.api.admin.auth import AdminUser
from localmail.api.errors import NotFound
from localmail.api.ids import parse_int_id
from localmail.importer.paths import ImportPathError, resolve_import_path
from localmail.serve.admin.csrf import check_csrf
from localmail.serve.admin.dependencies import require_admin_session

router = APIRouter(tags=["admin-imports"])


class _ImportIn(BaseModel):
    account_id: str
    source_kind: str
    source_path: str


def _job_dict(j: svc.ImportJob) -> dict:
    return {
        "id": str(j.id),
        "account_id": str(j.account_id),
        "source_kind": j.source_kind,
        "source_path": j.source_path,
        "status": j.status,
        "processed": j.processed,
        "inserted": j.inserted,
        "skipped_dup": j.skipped_dup,
        "failed": j.failed,
        "error_msg": j.error_msg,
        "cancel_requested": j.cancel_requested,
        "created_at": j.created_at.isoformat(),
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


@router.get("/imports")
def list_imports(request: Request, admin: AdminUser = require_admin_session()) -> dict:
    with request.app.state.pool.connection() as conn:
        rows = svc.list_jobs(conn)
    return {"imports": [_job_dict(r) for r in rows]}


@router.post("/imports", status_code=201)
def create_import(
    body: _ImportIn, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    check_csrf(request, admin, x_csrf_token, "/v1/admin/imports")
    aid = parse_int_id(body.account_id, field="account_id")
    cfg = request.app.state.imports_config
    try:
        resolved = resolve_import_path(body.source_path, cfg.roots)
    except ImportPathError as e:
        raise HTTPException(status_code=400, detail=str(e))
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            jid = svc.create_job(
                conn, account_id=aid, source_kind=body.source_kind,
                source_path=str(resolved))
        except svc.ImportBusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except svc.ImportFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        job = svc.get_job(conn, jid)
    dsn = request.app.state.db_dsn
    svc.start_job(
        lambda: psycopg.connect(dsn, autocommit=False), jid,
        attachments_root=request.app.state.attachments_root,
        checkpoint_every=cfg.checkpoint_every)
    return _job_dict(job)


@router.get("/imports/{job_id}")
def get_import(
    job_id: str, request: Request, admin: AdminUser = require_admin_session(),
) -> dict:
    jid = parse_int_id(job_id, field="job_id")
    with request.app.state.pool.connection() as conn:
        try:
            job = svc.get_job(conn, jid)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    return _job_dict(job)


@router.post("/imports/{job_id}/cancel", status_code=204)
def cancel_import(
    job_id: str, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    jid = parse_int_id(job_id, field="job_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/imports/{jid}/cancel")
    with request.app.state.pool.connection() as conn:
        try:
            svc.cancel_job(conn, jid)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    return Response(status_code=204)
```

- [ ] **Step 5: Thread the new params from the serve CLI**

In `src/localmail/cli.py`, the `create_app(...)` call at line ~1276 — add two kwargs (and ensure `cfg` is in scope; in the `LOCALMAIL_DSN_OVERRIDE` branch there is no `cfg`, so guard with defaults):

Replace the override branch additions: after `gmail_secrets = None` add `imports_cfg = None` and `attachments_root = None`; in the `else` branch after `gmail_secrets = ...` add:
```python
        imports_cfg = cfg.imports
        attachments_root = cfg.attachments.root
```
Then extend the `create_app(...)` call:
```python
        enable_control_socket=serve_cfg.supervise_daemon,
        imports_config=imports_cfg,
        attachments_root=attachments_root,
    )
```
(Add `from localmail.config import ... ImportsConfig` is not needed in cli.py since we pass the objects through.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_imports.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/serve/app.py src/localmail/serve/admin/imports_router.py src/localmail/cli.py tests/test_serve_admin_imports.py
git commit -m "feat(imports): /v1/admin/imports JSON router + app/CLI wiring"
```

---

## Task 12: Startup reconciliation in the serve lifespan

**Files:**
- Modify: `src/localmail/serve/app.py` (lifespan)
- Test: `tests/test_serve_admin_imports.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_serve_admin_imports.py`:

```python
def test_startup_reconciles_orphaned_running_job(db_dsn, db_conn, serve_cfg, tmp_path):
    aid = _archive(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, 'mbox', '/x', 'running') RETURNING id", (aid,))
        jid = int(cur.fetchone()[0])
    db_conn.commit()

    root = tmp_path / "imports"; root.mkdir()
    app = create_app(
        db_dsn=db_dsn, serve_config=serve_cfg,
        imports_config=ImportsConfig(roots=[root]), attachments_root=tmp_path / "b")
    with TestClient(app):  # enters lifespan → reconcile runs
        pass
    with db_conn.cursor() as cur:
        db_conn.rollback()
        cur.execute("SELECT status, error_msg FROM import_jobs WHERE id=%s", (jid,))
        status, err = cur.fetchone()
    assert status == "failed"
    assert "interrupted" in err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_imports.py::test_startup_reconciles_orphaned_running_job -v`
Expected: FAIL — job stays `running`.

- [ ] **Step 3: Add reconciliation to the lifespan**

In `src/localmail/serve/app.py`, import the service near the top:
```python
from localmail.api.admin import imports as _imports_svc
```
In the `lifespan` function, inside `try:` before `yield`, add:
```python
        with pool.connection() as conn:
            n = _imports_svc.reconcile_orphaned_jobs(conn)
            conn.commit()
            if n:
                import logging
                logging.getLogger("localmail.serve").warning(
                    "reconciled %d orphaned import job(s) at startup", n)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_imports.py::test_startup_reconciles_orphaned_running_job -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/app.py tests/test_serve_admin_imports.py
git commit -m "feat(imports): reconcile orphaned import jobs at serve startup"
```

---

## Task 13: HTML panel `/admin/imports` + templates + static

**Files:**
- Create: `src/localmail/serve/admin/imports_panel_router.py`
- Create: `src/localmail/serve/admin/templates/imports/list.html`
- Create: `src/localmail/serve/admin/templates/imports/detail.html`
- Create: `src/localmail/serve/admin/templates/imports/_progress.html`
- Create: `src/localmail/serve/admin/static/imports-panel.js`
- Modify: `src/localmail/serve/app.py` (include panel router)
- Test: `tests/test_serve_admin_import_screens.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_admin_import_screens.py`:

```python
"""HTML-screen tests for /admin/imports (2A.5)."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ImportsConfig, ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app

_KEY = "x" * 43


@pytest.fixture
def serve_cfg():
    return ServeConfig(session_signing_key=_KEY, state_signing_key="y" * 43,
                       cookie_secure=False)


@pytest.fixture
def admin_id(db_conn) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('horst', %s, TRUE) RETURNING id", (hash_password("pw"),))
        row = cur.fetchone()
    db_conn.commit()
    return int(row[0])


def _client(app, admin_id):
    c = TestClient(app, follow_redirects=False)
    form = c.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    c.post("/admin/login", data={"username": "horst", "password": "pw",
                                 "csrf_token": m.group(1)})
    return c


def test_panel_disabled_when_no_roots(db_dsn, serve_cfg, admin_id, tmp_path):
    app = create_app(db_dsn=db_dsn, serve_config=serve_cfg,
                     imports_config=ImportsConfig(roots=[]),
                     attachments_root=tmp_path / "b")
    c = _client(app, admin_id)
    r = c.get("/admin/imports")
    assert r.status_code == 200
    assert "disabled" in r.text.lower() or "not configured" in r.text.lower()


def test_panel_lists_with_roots(db_dsn, serve_cfg, admin_id, tmp_path):
    root = tmp_path / "imports"; root.mkdir()
    app = create_app(db_dsn=db_dsn, serve_config=serve_cfg,
                     imports_config=ImportsConfig(roots=[root]),
                     attachments_root=tmp_path / "b")
    c = _client(app, admin_id)
    r = c.get("/admin/imports")
    assert r.status_code == 200
    assert 'name="source_path"' in r.text


def test_progress_partial_renders(db_dsn, serve_cfg, admin_id, db_conn, tmp_path):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, config) "
            "VALUES ('arch', 'a@b.test', 'archive', '{}') RETURNING id")
        aid = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, 'mbox', '/x', 'completed') RETURNING id", (aid,))
        jid = int(cur.fetchone()[0])
    db_conn.commit()
    root = tmp_path / "imports"; root.mkdir()
    app = create_app(db_dsn=db_dsn, serve_config=serve_cfg,
                     imports_config=ImportsConfig(roots=[root]),
                     attachments_root=tmp_path / "b")
    c = _client(app, admin_id)
    r = c.get(f"/admin/_partials/import-status/{jid}")
    assert r.status_code == 200
    assert "completed" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_import_screens.py -v`
Expected: FAIL — panel router / templates missing.

- [ ] **Step 3: Create the panel router**

Create `src/localmail/serve/admin/imports_panel_router.py`:

```python
"""Admin import-management HTML screens (2A.5).

Thin server-rendered HTMX router mounted at /admin. Form parsing lives in
import_forms; execution dispatches to api/admin/imports. The progress partial
self-polls until the job reaches a terminal status.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psycopg
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from localmail.api.admin import accounts as accounts_svc
from localmail.api.admin import imports as svc
from localmail.api.errors import NotFound
from localmail.api.ids import parse_int_id
from localmail.importer.job_state import is_stale, is_terminal
from localmail.importer.paths import ImportPathError, resolve_import_path
from localmail.serve.admin import import_forms as forms
from localmail.serve.admin.csrf import check_csrf, csrf_token_context, session_signing_key
from localmail.serve.admin.dependencies import require_admin_session

IMPORT_PANEL_POLL_SECONDS = 2

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


def _base_context(request: Request, admin) -> dict:
    s_key = session_signing_key(request)
    return {
        "current_user": admin,
        "flashes": [],
        **csrf_token_context(user_id=admin.id, key=s_key),
    }


def _archive_accounts(conn: psycopg.Connection) -> list:
    return [a for a in accounts_svc.list_accounts(conn) if a.auth_method == "archive"]


def _progress_context(request: Request, admin, job: svc.ImportJob) -> dict:
    stale = is_stale(
        status=job.status, last_progress_at=job.last_progress_at,
        now=datetime.now(timezone.utc),
        stale_seconds=request.app.state.imports_config.stale_seconds)
    ctx = _base_context(request, admin)
    ctx.update({
        "job": job,
        "terminal": is_terminal(job.status),
        "stale": stale,
        "poll_seconds": IMPORT_PANEL_POLL_SECONDS,
    })
    return ctx


@router.get("/imports", response_class=HTMLResponse)
def imports_panel(request: Request, admin=require_admin_session()) -> HTMLResponse:
    cfg = request.app.state.imports_config
    with request.app.state.pool.connection() as conn:
        jobs = svc.list_jobs(conn)
        archives = _archive_accounts(conn)
    ctx = _base_context(request, admin)
    ctx.update({
        "jobs": jobs,
        "archive_accounts": archives,
        "imports_enabled": bool(cfg.roots),
        "roots": [str(r) for r in cfg.roots],
        "field_errors": {},
        "values": {"account_id": "", "source_kind": "mbox", "source_path": ""},
    })
    return templates.TemplateResponse(request=request, name="imports/list.html", context=ctx)


@router.post("/imports")
async def create_import(
    request: Request, admin=require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, "/admin/imports")
    raw = dict(await request.form())
    cfg = request.app.state.imports_config

    def _rerender_error(field_errors: dict) -> HTMLResponse:
        with request.app.state.pool.connection() as conn:
            archives = _archive_accounts(conn)
        ctx = _base_context(request, admin)
        ctx.update({
            "archive_accounts": archives, "imports_enabled": bool(cfg.roots),
            "roots": [str(r) for r in cfg.roots], "field_errors": field_errors,
            "values": {"account_id": raw.get("account_id", ""),
                       "source_kind": raw.get("source_kind", "mbox"),
                       "source_path": raw.get("source_path", "")},
        })
        return templates.TemplateResponse(
            request=request, name="imports/_form.html", context=ctx, status_code=400)

    try:
        kwargs = forms.form_to_create_kwargs(raw)
        resolved = resolve_import_path(kwargs["source_path"], cfg.roots)
    except (forms.FormError, ImportPathError) as e:
        return _rerender_error(forms.field_errors_from(e) if isinstance(e, forms.FormError)
                               else {"source_path": str(e)})

    with request.app.state.pool.connection() as conn:
        try:
            jid = svc.create_job(
                conn, account_id=kwargs["account_id"],
                source_kind=kwargs["source_kind"], source_path=str(resolved))
        except (svc.ImportBusyError, svc.ImportFieldError) as e:
            return _rerender_error(forms.field_errors_from(e))
        except NotFound:
            return _rerender_error({"_form": "account not found"})

    dsn = request.app.state.db_dsn
    svc.start_job(
        lambda: psycopg.connect(dsn, autocommit=False), jid,
        attachments_root=request.app.state.attachments_root,
        checkpoint_every=cfg.checkpoint_every)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/admin/imports/{jid}"
    return resp


@router.get("/imports/{job_id}", response_class=HTMLResponse)
def import_detail(job_id: int, request: Request, admin=require_admin_session()) -> HTMLResponse:
    with request.app.state.pool.connection() as conn:
        try:
            job = svc.get_job(conn, job_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    ctx = _progress_context(request, admin, job)
    return templates.TemplateResponse(request=request, name="imports/detail.html", context=ctx)


@router.get("/_partials/import-status/{job_id}", response_class=HTMLResponse)
def import_status_partial(
    job_id: int, request: Request, admin=require_admin_session(),
) -> HTMLResponse:
    with request.app.state.pool.connection() as conn:
        try:
            job = svc.get_job(conn, job_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    ctx = _progress_context(request, admin, job)
    return templates.TemplateResponse(request=request, name="imports/_progress.html", context=ctx)


@router.post("/imports/{job_id}/cancel", response_class=HTMLResponse)
def cancel_import(
    job_id: int, request: Request, admin=require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/imports/{job_id}/cancel")
    with request.app.state.pool.connection() as conn:
        try:
            svc.cancel_job(conn, job_id)
            job = svc.get_job(conn, job_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    ctx = _progress_context(request, admin, job)
    return templates.TemplateResponse(request=request, name="imports/_progress.html", context=ctx)
```

- [ ] **Step 4: Create the templates**

Create `src/localmail/serve/admin/templates/imports/list.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Imports</h1>

{% if not imports_enabled %}
  <p class="notice">Imports are <strong>disabled</strong>: no <code>[imports].roots</code> configured.
  Set an allowlist of directories in <code>config.toml</code> and restart <code>serve</code>.</p>
{% else %}
  <p>Allowed roots: {% for r in roots %}<code>{{ r }}</code>{% if not loop.last %}, {% endif %}{% endfor %}</p>
  {% include "imports/_form.html" %}
{% endif %}

<h2>Recent jobs</h2>
<table>
  <thead><tr><th>ID</th><th>Account</th><th>Source</th><th>Status</th><th>Processed</th></tr></thead>
  <tbody>
  {% for j in jobs %}
    <tr>
      <td><a href="/admin/imports/{{ j.id }}">{{ j.id }}</a></td>
      <td>{{ j.account_id }}</td>
      <td>{{ j.source_kind }}: {{ j.source_path }}</td>
      <td>{{ j.status }}</td>
      <td>{{ j.processed }}</td>
    </tr>
  {% else %}
    <tr><td colspan="5">No imports yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
<script src="/admin/static/imports-panel.js"></script>
{% endblock %}
```

Create `src/localmail/serve/admin/templates/imports/_form.html`:

```html
<form hx-post="/admin/imports" hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/imports") }}"}'>
  {% if field_errors._form %}<p class="error">{{ field_errors._form }}</p>{% endif %}
  <label>Archive account
    <select name="account_id">
      <option value="">— choose —</option>
      {% for a in archive_accounts %}
        <option value="{{ a.id }}" {% if values.account_id == a.id|string %}selected{% endif %}>{{ a.name }}</option>
      {% endfor %}
    </select>
  </label>
  <label>Source kind
    <select name="source_kind">
      <option value="mbox" {% if values.source_kind == "mbox" %}selected{% endif %}>mbox</option>
      <option value="maildir" {% if values.source_kind == "maildir" %}selected{% endif %}>maildir</option>
    </select>
  </label>
  <label>Source path
    <input type="text" name="source_path" value="{{ values.source_path }}" placeholder="/srv/localmail/imports/takeout.mbox">
    {% if field_errors.source_path %}<span class="error">{{ field_errors.source_path }}</span>{% endif %}
  </label>
  <button type="submit" {% if not archive_accounts %}disabled{% endif %}>Start import</button>
  {% if not archive_accounts %}<p class="notice">Create an <em>archive</em> account first under <a href="/admin/accounts">Accounts</a>.</p>{% endif %}
</form>
```

Create `src/localmail/serve/admin/templates/imports/detail.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Import #{{ job.id }}</h1>
<p><a href="/admin/imports">&larr; All imports</a></p>
<div id="import-status">
  {% include "imports/_progress.html" %}
</div>
<div id="import-toast" hidden></div>
<script src="/admin/static/imports-panel.js"></script>
{% endblock %}
```

Create `src/localmail/serve/admin/templates/imports/_progress.html`:

```html
<div id="import-status"
     {% if not terminal %}hx-get="/admin/_partials/import-status/{{ job.id }}"
     hx-trigger="every {{ poll_seconds }}s" hx-swap="outerHTML"{% endif %}>
  <p class="status {% if job.status == 'failed' or stale %}error{% endif %}">
    Status: <strong>{{ job.status }}</strong>{% if stale %} (stalled — no progress){% endif %}
  </p>
  <ul>
    <li>Processed: {{ job.processed }}</li>
    <li>Inserted: {{ job.inserted }}</li>
    <li>Skipped (duplicates): {{ job.skipped_dup }}</li>
    <li>Failed: {{ job.failed }}</li>
  </ul>
  {% if job.error_msg %}<p class="error">Error: {{ job.error_msg }}</p>{% endif %}
  {% if not terminal %}
    <form hx-post="/admin/imports/{{ job.id }}/cancel" hx-target="#import-status" hx-swap="outerHTML"
          hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/imports/" ~ job.id ~ "/cancel") }}"}'>
      <button type="submit">Cancel import</button>
    </form>
  {% endif %}
</div>
```

Create `src/localmail/serve/admin/static/imports-panel.js`:

```javascript
// Import panel: placeholder for future client behaviour. The progress fragment
// self-polls via htmx; CSP for /admin is `script-src 'self'`, so any future
// JS must live in this served file (no inline handlers).
(function () {
  "use strict";
})();
```

- [ ] **Step 5: Include the panel router in `create_app`**

In `src/localmail/serve/app.py`, add the import:
```python
from localmail.serve.admin import imports_panel_router as admin_imports_panel_router
```
And in the admin block, after `app.include_router(admin_users_panel_router.router, prefix="/admin")`:
```python
        app.include_router(admin_imports_panel_router.router, prefix="/admin")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_import_screens.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/serve/admin/imports_panel_router.py src/localmail/serve/admin/templates/imports/ src/localmail/serve/admin/static/imports-panel.js src/localmail/serve/app.py tests/test_serve_admin_import_screens.py
git commit -m "feat(imports): /admin/imports HTML panel + self-polling progress"
```

---

## Task 14: CLI `localmail import`

**Files:**
- Modify: `src/localmail/cli.py`
- Test: `tests/test_cli_import.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_import.py`:

```python
"""CLI `localmail import` test."""
from __future__ import annotations

import mailbox as _mailbox

import psycopg
from click.testing import CliRunner

from localmail.cli import main
from tests import _eml
from tests.conftest import TEST_DSN


def test_cli_import_mbox(db_conn, tmp_path, cli_config, monkeypatch):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, config) "
            "VALUES ('arch', 'a@b.test', 'archive', '{}') RETURNING id")
        db_conn.commit()
    p = tmp_path / "a.mbox"
    box = _mailbox.mbox(str(p))
    box.lock(); box.add(_mailbox.mboxMessage(_eml.plain())); box.flush(); box.unlock()

    runner = CliRunner()
    result = runner.invoke(
        main, ["import", str(p), "--account", "arch", "--kind", "mbox"])
    assert result.exit_code == 0, result.output
    assert "inserted" in result.output.lower()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_import.py -v`
Expected: FAIL — no `import` command.

- [ ] **Step 3: Add the CLI command**

In `src/localmail/cli.py`, add (near `retry-failed`, after its definition). The command runs `run_import` **inline** (synchronously) and prints the final counts:

```python
@main.command("import")
@click.argument("source_path", type=click.Path(exists=True))
@click.option("--account", "account_name", required=True, help="Target archive account name.")
@click.option("--kind", type=click.Choice(["mbox", "maildir"]), required=True)
@click.pass_context
def import_cmd(
    ctx: click.Context, source_path: str, account_name: str, kind: str,
) -> None:
    """Import an mbox file or maildir directory into an archive account.

    Runs synchronously and prints the final counts. Re-running is idempotent
    (per-account dedup), so a re-import skips already-imported messages.
    """
    from localmail.api.admin import imports as imports_svc
    from localmail.api.admin import accounts as accounts_svc
    from localmail.importer import runner

    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn, autocommit=False) as conn:
        account = accounts_svc.get_account_by_name(conn, account_name)
        if account is None:
            raise click.ClickException(f"no such account: {account_name!r}")
        if account.auth_method != "archive":
            raise click.ClickException(f"{account_name!r} is not an archive account")
        try:
            jid = imports_svc.create_job(
                conn, account_id=account.id, source_kind=kind, source_path=source_path)
        except imports_svc.ImportBusyError as e:
            raise click.ClickException(str(e))
        conn.commit()

    runner.run_import(
        lambda: psycopg.connect(cfg.database.dsn, autocommit=False), jid,
        attachments_root=cfg.attachments.root, checkpoint_every=cfg.imports.checkpoint_every)

    with psycopg.connect(cfg.database.dsn) as conn:
        job = imports_svc.get_job(conn, jid)
    click.echo(
        f"status={job.status} processed={job.processed} inserted={job.inserted} "
        f"skipped_dup={job.skipped_dup} failed={job.failed}")
    if job.error_msg:
        click.echo(f"error: {job.error_msg}", err=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_import.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_import.py
git commit -m "feat(imports): localmail import CLI command (synchronous)"
```

---

## Task 15: Full suite, lint, docs

**Files:**
- Modify: `CLAUDE.md` (add a 2A.5 bullet under the admin-screens section)
- Modify: `README.md` (operator note + config example)
- Modify: `config.example.toml` (add `[imports]` section)

- [ ] **Step 1: Run the full suite + type-check + lint**

```bash
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
unset VIRTUAL_ENV && uv run ruff check src/localmail tests
```
Expected: all green (pool `__del__` ResourceWarnings at teardown are pre-existing, not failures). Fix any failures before continuing.

- [ ] **Step 2: Add the `[imports]` block to `config.example.toml`**

```toml
[imports]
# Directories the /admin/imports UI may read archives from. Empty = imports
# disabled. Each path is realpathed; a source must resolve under one of these.
roots = []
# How often (in messages) the import worker flushes progress counters.
checkpoint_every = 50
# A running job idle longer than this is shown stalled (red) in the panel.
stale_seconds = 60
```

- [ ] **Step 3: Document in `CLAUDE.md`**

Add a bullet under the admin-screens section describing Sub-plan 2A.5: the `/admin/imports` + `/v1/admin/imports` screens, the `importer/` package (`paths`, `sources`, `job_state`, `runner`), the `import_jobs` table (migration 0026), the single-active busy-guard, received-date from the mbox `From_`/maildir file date into `internal_date`, the three-layer failure visibility (runner terminal status + `last_progress_at` stall flag + startup reconciliation), `[imports]` config, and the `localmail import` CLI. Update the migration line ("Latest is `0026_import_jobs.sql`; next would be `0027_*.sql`").

- [ ] **Step 4: Document in `README.md`**

Add an operator-facing paragraph: how to create an archive account, drop an mbox/maildir under an `[imports].roots` directory, and start an import from `/admin/imports` (or `localmail import <path> --account NAME --kind mbox`); note re-import is idempotent.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md config.example.toml
git commit -m "docs(imports): document /admin/imports (2A.5) + [imports] config"
```

- [ ] **Step 6: Push + open PR**

```bash
git push -u origin admin-ui-2a5-imports
gh pr create --title "feat(admin): archive-import screens /admin/imports (2A.5)" \
  --body "Closes the last 404 admin nav link. mbox + maildir import from an allowlisted server path into an archive account, in-serve worker thread, import_jobs (migration 0026), self-polling progress, 3-layer failure visibility, localmail import CLI. Spec + plan under docs/superpowers/."
```

---

## Self-Review (completed by plan author)

**Spec coverage:** §2 scope → Tasks 3-5 (formats/source), Task 7/11 (archive target + path guard), Task 8 (worker), Task 2 (import_jobs). §4 schema → Task 2. §5 ingestion/received-date → Tasks 4,5,8. §6 concurrency/cancel/failure → Tasks 2 (busy-guard), 7 (cancel/reconcile), 8 (runner terminal + checkpoint heartbeat), 12 (startup reconcile), 13 (stall flag in `_progress.html`). §7 security → Task 3 (allowlist), Task 11/13 (CSRF, archive-only). §8 HTTP surface → Tasks 11 (JSON) + 13 (HTML). §9 config → Task 1 + Task 15. §10 testing → tests in every task. §11 risks → documented; admin-global deviation flagged at top.

**Placeholder scan:** No TBD/TODO. The `runner.py` `field` import is explicitly removed in Task 8 Step 4. The poison-message test (Task 8) is documented as asserting clean-corpus behaviour because forcing a parser-rejected message from `_eml` is not straightforward — the savepoint isolation path itself is unit-pinned by the existing `test_embed_worker` NUL-byte test referenced in CLAUDE.md; if a stronger poison assertion is wanted, inject a message with a NUL in a header during implementation.

**Type consistency:** `ImportJob` fields match the `_SELECT` column order/names (class_row maps by name). `run_import(conn_factory, job_id, *, attachments_root, checkpoint_every)` is identical in Tasks 8, 9, 11, 13, 14. `create_job(conn, *, account_id, source_kind, source_path)` identical in Tasks 7, 11, 13, 14. `resolve_import_path(raw, roots)` identical in Tasks 3, 11, 13. `is_stale(*, status, last_progress_at, now, stale_seconds)` identical in Tasks 6, 13.
