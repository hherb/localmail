# Daemon Heartbeats (2B.2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the localmail daemon per-thread liveness heartbeats persisted in Postgres so an operator (and, later, the 2B.4 supervisor / 2B.5 admin panel) can see whether each account's IDLE + poll threads and the process-level embed / extract / reconcile workers are alive, what they're doing, and whether any has gone stale.

**Architecture:** Plane A (DB-mediated, supervisor-agnostic). A new `daemon_heartbeats` table holds one row per `(worker_kind, account_id)` thread and one per process-level worker (`account_id IS NULL`). A tiny pure writer (`heartbeat.py`) upserts the row; each daemon loop calls a never-raising `safe_heartbeat(pool, …)` wrapper at the top of every iteration and on state transitions. A read-only service accessor (`api/admin/daemon.py::get_daemon_status`) derives `stale` purely from `now() - last_heartbeat_at > daemon.heartbeat_stale_seconds`. The daemon `DELETE`s all rows once at startup (single-instance assumption). **No HTTP route and no CLI in this slice** — read accessor only; the route/CLI land in 2B.4.

**Tech Stack:** Python ≥3.12, psycopg v3 + raw SQL, numbered `.sql` migration, pydantic v2 config, pytest against real Postgres (`LOCALMAIL_TEST_DSN`).

**Spec:** [docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](../specs/2026-05-30-daemon-control-2b-respec-design.md) §2B.2.

---

## File structure

| File | Responsibility | New? |
|------|----------------|------|
| `migrations/0023_daemon_heartbeats.sql` | `daemon_heartbeats` table + two partial unique indexes | NEW |
| `src/localmail/heartbeat.py` | `record_heartbeat`, `clear_all_heartbeats` (pure, take a conn), `safe_heartbeat` (pool wrapper, never raises), `WorkerKind`/`WorkerState` Literals | NEW |
| `src/localmail/api/admin/daemon.py` | `HeartbeatRow`, `DaemonStatus`, `get_daemon_status(conn, *, stale_seconds)` read accessor | NEW |
| `src/localmail/config.py` | `DaemonConfig.heartbeat_stale_seconds` (int, 120) | modify |
| `src/localmail/daemon.py` | startup `clear_all_heartbeats`; `reconcile` writes a `reconcile` heartbeat each tick | modify |
| `src/localmail/idle.py` | `idle` heartbeats (connecting / idle / syncing / reconnecting) | modify |
| `src/localmail/poller.py` | `poll` heartbeats (polling / syncing+folder / reconnecting) | modify |
| `src/localmail/search/embed_worker.py` | `embed` process heartbeat (idle / error) | modify |
| `src/localmail/search/extract_worker.py` | `extract` process heartbeat (idle / error) | modify |
| `tests/test_migration_0023.py` | migration apply / shape / partial-index test | NEW |
| `tests/test_heartbeat.py` | writer upsert on both conflict targets; clear; safe wrapper swallows | NEW |
| `tests/test_admin_daemon.py` | `get_daemon_status` staleness derivation + ordering | NEW |
| `tests/test_config.py` | `heartbeat_stale_seconds` default + override | modify |
| `tests/test_daemon_heartbeats_wiring.py` | idle/poll/embed/extract/reconcile call heartbeat with right kind+state | NEW |
| `tests/conftest.py` | add `daemon_heartbeats` to the TRUNCATE list | modify |
| `config.example.toml` | document `heartbeat_stale_seconds` | modify |
| `README.md` | one-line note that the daemon records heartbeats | modify |

State vocabulary (constrained by the SQL CHECK and the `WorkerState` Literal):
`starting`, `connecting`, `idle`, `polling`, `syncing`, `error`, `reconnecting`, `stopped`.
Worker kinds (`WorkerKind` Literal + CHECK): `idle`, `poll`, `embed`, `extract`, `reconcile`.

---

### Task 1: Migration `0023_daemon_heartbeats.sql` + apply test + conftest TRUNCATE

**Files:**
- Create: `migrations/0023_daemon_heartbeats.sql`
- Create: `tests/test_migration_0023.py`
- Modify: `tests/conftest.py` (TRUNCATE list)

- [ ] **Step 1: Write the failing migration-apply test**

Create `tests/test_migration_0023.py`:

```python
"""Migration 0023 adds daemon_heartbeats + two partial unique indexes."""
from __future__ import annotations

import psycopg


def test_daemon_heartbeats_table_shape(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'daemon_heartbeats' "
            "ORDER BY ordinal_position"
        )
        cols = {name: (dtype, nullable) for name, dtype, nullable in cur.fetchall()}
    assert cols, "daemon_heartbeats table missing"
    assert cols["worker_kind"] == ("text", "NO")
    assert cols["account_id"] == ("integer", "YES")
    assert cols["state"] == ("text", "NO")
    assert cols["current_folder"] == ("text", "YES")
    assert cols["last_error_msg"] == ("text", "YES")
    assert cols["started_at"][1] == "NO"
    assert cols["last_heartbeat_at"][1] == "NO"


def test_partial_unique_indexes_exist(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename = 'daemon_heartbeats'"
        )
        defs = {name: ddl for name, ddl in cur.fetchall()}
    acct = defs.get("daemon_heartbeats_acct_idx")
    proc = defs.get("daemon_heartbeats_proc_idx")
    assert acct is not None and "UNIQUE" in acct
    assert "worker_kind" in acct and "account_id" in acct
    assert "account_id IS NOT NULL" in acct
    assert proc is not None and "UNIQUE" in proc
    assert "account_id IS NULL" in proc


def test_worker_kind_check_rejects_unknown(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO daemon_heartbeats "
                "(worker_kind, account_id, state, started_at, last_heartbeat_at) "
                "VALUES ('bogus', NULL, 'idle', now(), now())"
            )
            raised = False
        except psycopg.errors.CheckViolation:
            raised = True
        db_conn.rollback()
    assert raised, "worker_kind CHECK did not reject unknown value"
```

- [ ] **Step 2: Run it to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_migration_0023.py -q`
Expected: FAIL — `daemon_heartbeats` table does not exist yet (the session-scoped `db_dsn` fixture applies migrations, so once the file exists this passes).

- [ ] **Step 3: Write the migration**

Create `migrations/0023_daemon_heartbeats.sql`:

```sql
-- Daemon liveness heartbeats (2B.2, Plane A — DB-mediated, supervisor-agnostic).
-- One row per account thread (worker_kind in idle/poll, account_id NOT NULL) and
-- one per process-level worker (embed/extract/reconcile, account_id NULL). Each
-- worker upserts its row at the top of every loop iteration and on state
-- transitions; the admin daemon-status reader derives liveness purely from
-- now() - last_heartbeat_at. Multi-host clustering is a non-goal, so the daemon
-- DELETEs every row once at startup (see localmail.heartbeat.clear_all_heartbeats)
-- — leftover rows from a crashed previous run never read as live.

CREATE TABLE daemon_heartbeats (
    id                BIGSERIAL    PRIMARY KEY,
    worker_kind       TEXT         NOT NULL
                                   CHECK (worker_kind IN
                                          ('idle','poll','embed','extract','reconcile')),
    account_id        INT          REFERENCES accounts(id) ON DELETE CASCADE,
    state             TEXT         NOT NULL
                                   CHECK (state IN
                                          ('starting','connecting','idle','polling',
                                           'syncing','error','reconnecting','stopped')),
    current_folder    TEXT,
    last_error_msg    TEXT,
    started_at        TIMESTAMPTZ  NOT NULL,
    last_heartbeat_at TIMESTAMPTZ  NOT NULL
);

-- Two partial unique indexes (instead of UNIQUE NULLS NOT DISTINCT) keep this
-- Postgres-version-agnostic. Each is a valid ON CONFLICT target.
CREATE UNIQUE INDEX daemon_heartbeats_acct_idx
    ON daemon_heartbeats (worker_kind, account_id) WHERE account_id IS NOT NULL;

CREATE UNIQUE INDEX daemon_heartbeats_proc_idx
    ON daemon_heartbeats (worker_kind) WHERE account_id IS NULL;
```

- [ ] **Step 4: Add `daemon_heartbeats` to the conftest TRUNCATE list**

In `tests/conftest.py`, the `db_conn` fixture TRUNCATE statement — append `daemon_heartbeats` to the comma-separated table list (it must be truncated between tests so heartbeat rows don't leak across cases):

```python
            cur.execute(
                "TRUNCATE accounts, mailboxes, messages, message_labels, "
                "attachment_blobs, failed_messages, message_chunks, "
                "failed_embeddings, embedding_models, failed_chunkings, "
                "attachment_text, attachment_chunks, failed_extractions, "
                "api_users, api_tokens, user_accounts, api_login_attempts, "
                "daemon_heartbeats RESTART IDENTITY CASCADE"
            )
```

- [ ] **Step 5: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_migration_0023.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add migrations/0023_daemon_heartbeats.sql tests/test_migration_0023.py tests/conftest.py
git commit -m "feat(daemon): daemon_heartbeats table + partial unique indexes (2B.2)"
```

---

### Task 2: `heartbeat.py` — `record_heartbeat` + `clear_all_heartbeats`

**Files:**
- Create: `src/localmail/heartbeat.py`
- Create: `tests/test_heartbeat.py`

- [ ] **Step 1: Write the failing writer tests**

Create `tests/test_heartbeat.py`:

```python
"""Heartbeat writer: upsert on both partial-index targets; clear-all."""
from __future__ import annotations

import psycopg

from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account
from localmail.config import AccountConfig
from localmail.heartbeat import clear_all_heartbeats, record_heartbeat


def _account(conn: psycopg.Connection, name: str = "acct") -> int:
    cfg = AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993, auth_method="password",
    )
    return create_account(conn, **account_create_kwargs(cfg)).id


def _rows(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT worker_kind, account_id, state, current_folder, last_error_msg "
            "FROM daemon_heartbeats ORDER BY account_id NULLS LAST, worker_kind"
        )
        return cur.fetchall()


def test_account_heartbeat_insert_then_update_same_row(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="connecting")
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle",
                     current_folder="INBOX")
    db_conn.commit()
    rows = _rows(db_conn)
    assert rows == [("idle", aid, "idle", "INBOX", None)]  # one row, updated in place


def test_two_account_threads_are_distinct_rows(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle")
    record_heartbeat(db_conn, worker_kind="poll", account_id=aid, state="polling")
    db_conn.commit()
    rows = _rows(db_conn)
    assert {(k, a) for k, a, *_ in rows} == {("idle", aid), ("poll", aid)}


def test_process_heartbeat_insert_then_update_same_row(db_conn: psycopg.Connection) -> None:
    record_heartbeat(db_conn, worker_kind="embed", account_id=None, state="idle")
    record_heartbeat(db_conn, worker_kind="embed", account_id=None, state="error",
                     last_error_msg="boom")
    db_conn.commit()
    rows = _rows(db_conn)
    assert rows == [("embed", None, "error", None, "boom")]  # one row, updated


def test_started_at_is_preserved_across_updates(db_conn: psycopg.Connection) -> None:
    record_heartbeat(db_conn, worker_kind="reconcile", account_id=None, state="idle")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT started_at FROM daemon_heartbeats WHERE worker_kind='reconcile'")
        first = cur.fetchone()[0]
    record_heartbeat(db_conn, worker_kind="reconcile", account_id=None, state="idle")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT started_at, last_heartbeat_at FROM daemon_heartbeats "
                    "WHERE worker_kind='reconcile'")
        started_at, last_hb = cur.fetchone()
    assert started_at == first  # started_at frozen on first insert
    assert last_hb >= started_at  # last_heartbeat_at advances


def test_clear_all_heartbeats_empties_table(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle")
    record_heartbeat(db_conn, worker_kind="embed", account_id=None, state="idle")
    db_conn.commit()
    clear_all_heartbeats(db_conn)
    db_conn.commit()
    assert _rows(db_conn) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_heartbeat.py -q`
Expected: FAIL — `ModuleNotFoundError: localmail.heartbeat`.

- [ ] **Step 3: Write `heartbeat.py`**

Create `src/localmail/heartbeat.py`:

```python
"""Daemon liveness heartbeats (2B.2, Plane A).

Every daemon worker thread (per-account IDLE + poll) and process-level worker
(embed, extract, reconcile) upserts a single `daemon_heartbeats` row at the top
of each loop iteration and on state transitions. The admin daemon-status reader
(api/admin/daemon.py) derives liveness purely from now() - last_heartbeat_at.

`record_heartbeat` / `clear_all_heartbeats` are pure-ish: they take a conn and
do NOT commit (the caller owns the transaction). `safe_heartbeat` borrows a pool
connection, records, commits, and swallows every exception — a heartbeat write
must never crash the sync/poll loop that calls it.
"""
from __future__ import annotations

import logging
from typing import Literal

import psycopg
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

WorkerKind = Literal["idle", "poll", "embed", "extract", "reconcile"]
WorkerState = Literal[
    "starting", "connecting", "idle", "polling",
    "syncing", "error", "reconnecting", "stopped",
]

_UPSERT_ACCOUNT = """
    INSERT INTO daemon_heartbeats
        (worker_kind, account_id, state, current_folder,
         last_error_msg, started_at, last_heartbeat_at)
    VALUES (%s, %s, %s, %s, %s, now(), now())
    ON CONFLICT (worker_kind, account_id) WHERE account_id IS NOT NULL
    DO UPDATE SET state = EXCLUDED.state,
                  current_folder = EXCLUDED.current_folder,
                  last_error_msg = EXCLUDED.last_error_msg,
                  last_heartbeat_at = now()
"""

_UPSERT_PROCESS = """
    INSERT INTO daemon_heartbeats
        (worker_kind, account_id, state, current_folder,
         last_error_msg, started_at, last_heartbeat_at)
    VALUES (%s, NULL, %s, %s, %s, now(), now())
    ON CONFLICT (worker_kind) WHERE account_id IS NULL
    DO UPDATE SET state = EXCLUDED.state,
                  current_folder = EXCLUDED.current_folder,
                  last_error_msg = EXCLUDED.last_error_msg,
                  last_heartbeat_at = now()
"""


def record_heartbeat(
    conn: psycopg.Connection,
    *,
    worker_kind: WorkerKind,
    account_id: int | None,
    state: WorkerState,
    current_folder: str | None = None,
    last_error_msg: str | None = None,
) -> None:
    """Upsert this worker's heartbeat row. Does NOT commit (caller owns the tx).

    Account-scoped workers (idle/poll) pass an account_id and hit the
    `daemon_heartbeats_acct_idx` partial index; process-level workers
    (embed/extract/reconcile) pass None and hit `daemon_heartbeats_proc_idx`.
    `started_at` is set only on insert (the DO UPDATE never touches it).
    """
    if account_id is None:
        conn.execute(_UPSERT_PROCESS,
                     (worker_kind, state, current_folder, last_error_msg))
    else:
        conn.execute(_UPSERT_ACCOUNT,
                     (worker_kind, account_id, state, current_folder, last_error_msg))


def clear_all_heartbeats(conn: psycopg.Connection) -> None:
    """Delete every heartbeat row (single-instance startup reset). No commit."""
    conn.execute("DELETE FROM daemon_heartbeats")


def safe_heartbeat(
    pool: ConnectionPool,
    *,
    worker_kind: WorkerKind,
    account_id: int | None,
    state: WorkerState,
    current_folder: str | None = None,
    last_error_msg: str | None = None,
) -> None:
    """Borrow a pool connection, record a heartbeat, commit. Never raises.

    All exceptions are logged at WARNING and swallowed so a transient DB blip
    or a heartbeat-write bug can never crash the long-lived loop that calls it.
    """
    try:
        with pool.connection() as conn:
            record_heartbeat(
                conn, worker_kind=worker_kind, account_id=account_id,
                state=state, current_folder=current_folder,
                last_error_msg=last_error_msg,
            )
    except Exception:  # noqa: BLE001
        log.warning("heartbeat write failed (kind=%s account_id=%s)",
                    worker_kind, account_id, exc_info=True)
```

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_heartbeat.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Add the `safe_heartbeat`-swallows test**

Append to `tests/test_heartbeat.py`:

```python
def test_safe_heartbeat_swallows_pool_errors() -> None:
    from localmail.heartbeat import safe_heartbeat

    class _BoomPool:
        def connection(self):  # noqa: D401
            raise RuntimeError("pool exhausted")

    # Must not raise — a heartbeat failure can't be allowed to kill the loop.
    safe_heartbeat(_BoomPool(), worker_kind="idle", account_id=1, state="idle")
```

- [ ] **Step 6: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_heartbeat.py -q`
Expected: PASS (6 tests).

- [ ] **Step 7: Commit**

```bash
git add src/localmail/heartbeat.py tests/test_heartbeat.py
git commit -m "feat(daemon): heartbeat writer (record/clear/safe) (2B.2)"
```

---

### Task 3: `DaemonConfig.heartbeat_stale_seconds`

**Files:**
- Modify: `src/localmail/config.py` (`DaemonConfig`)
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write the failing config test**

Append to `tests/test_config.py`:

```python
def test_daemon_heartbeat_stale_seconds_default() -> None:
    from localmail.config import DaemonConfig

    assert DaemonConfig().heartbeat_stale_seconds == 120


def test_daemon_heartbeat_stale_seconds_override() -> None:
    from localmail.config import DaemonConfig

    assert DaemonConfig(heartbeat_stale_seconds=45).heartbeat_stale_seconds == 45
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config.py -q -k heartbeat_stale`
Expected: FAIL — `heartbeat_stale_seconds` is not a field.

- [ ] **Step 3: Add the field**

In `src/localmail/config.py`, inside `class DaemonConfig`, after `shutdown_grace_seconds`:

```python
    # 2B.2 heartbeats: a worker's heartbeat is "stale" when
    # now() - last_heartbeat_at exceeds this. Default comfortably exceeds the
    # ~30s IDLE heartbeat tick (idle.HEARTBEAT_SECONDS) so a healthy worker is
    # never flagged stale by jitter.
    heartbeat_stale_seconds: int = 120
```

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config.py -q -k heartbeat_stale`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_config.py
git commit -m "feat(config): daemon.heartbeat_stale_seconds (2B.2)"
```

---

### Task 4: `api/admin/daemon.py` — `get_daemon_status` reader

**Files:**
- Create: `src/localmail/api/admin/daemon.py`
- Create: `tests/test_admin_daemon.py`

- [ ] **Step 1: Write the failing reader tests**

Create `tests/test_admin_daemon.py`:

```python
"""Daemon-status read accessor: staleness derivation + ordering."""
from __future__ import annotations

import psycopg

from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account
from localmail.api.admin.daemon import get_daemon_status
from localmail.config import AccountConfig
from localmail.heartbeat import record_heartbeat


def _account(conn: psycopg.Connection, name: str = "acct") -> int:
    cfg = AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993, auth_method="password",
    )
    return create_account(conn, **account_create_kwargs(cfg)).id


def test_empty_status_when_no_heartbeats(db_conn: psycopg.Connection) -> None:
    status = get_daemon_status(db_conn, stale_seconds=120)
    assert status.heartbeats == []


def test_fresh_heartbeat_is_not_stale(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle")
    db_conn.commit()
    status = get_daemon_status(db_conn, stale_seconds=120)
    assert len(status.heartbeats) == 1
    hb = status.heartbeats[0]
    assert hb.worker_kind == "idle"
    assert hb.account_id == aid
    assert hb.state == "idle"
    assert hb.stale is False


def test_old_heartbeat_is_stale(db_conn: psycopg.Connection) -> None:
    record_heartbeat(db_conn, worker_kind="embed", account_id=None, state="idle")
    db_conn.commit()
    # Force the row's last_heartbeat_at into the past.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE daemon_heartbeats "
            "SET last_heartbeat_at = now() - interval '10 minutes' "
            "WHERE worker_kind = 'embed'"
        )
    db_conn.commit()
    status = get_daemon_status(db_conn, stale_seconds=120)
    assert len(status.heartbeats) == 1
    assert status.heartbeats[0].stale is True


def test_rows_ordered_account_first_then_kind(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="poll", account_id=aid, state="polling")
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle")
    record_heartbeat(db_conn, worker_kind="reconcile", account_id=None, state="idle")
    db_conn.commit()
    status = get_daemon_status(db_conn, stale_seconds=120)
    kinds = [(hb.account_id, hb.worker_kind) for hb in status.heartbeats]
    # account rows first (idle before poll), process row (NULL account) last
    assert kinds == [(aid, "idle"), (aid, "poll"), (None, "reconcile")]
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_daemon.py -q`
Expected: FAIL — `ModuleNotFoundError: localmail.api.admin.daemon`.

- [ ] **Step 3: Write the reader**

Create `src/localmail/api/admin/daemon.py`:

```python
"""Service layer for daemon status (2B.2).

Pure read accessor over a psycopg connection — no FastAPI, no IO beyond the
conn. Daemon status is operator-global (no per-user ACL); the HTTP route that
exposes it (2B.4) is admin-gated. Staleness is derived in SQL from
now() - last_heartbeat_at so it can't drift from the writer's clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row


@dataclass(frozen=True)
class HeartbeatRow:
    worker_kind: str
    account_id: int | None
    state: str
    current_folder: str | None
    last_error_msg: str | None
    started_at: datetime
    last_heartbeat_at: datetime
    stale: bool


@dataclass(frozen=True)
class DaemonStatus:
    heartbeats: list[HeartbeatRow]


def get_daemon_status(
    conn: psycopg.Connection, *, stale_seconds: int
) -> DaemonStatus:
    """Return every heartbeat row with a derived `stale` flag, account rows
    first (then NULL-account process rows), ordered by worker_kind within."""
    with conn.cursor(row_factory=class_row(HeartbeatRow)) as cur:
        cur.execute(
            """
            SELECT worker_kind, account_id, state, current_folder,
                   last_error_msg, started_at, last_heartbeat_at,
                   (now() - last_heartbeat_at) > make_interval(secs => %s) AS stale
              FROM daemon_heartbeats
             ORDER BY account_id NULLS LAST, worker_kind
            """,
            (stale_seconds,),
        )
        return DaemonStatus(heartbeats=cur.fetchall())
```

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_daemon.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/daemon.py tests/test_admin_daemon.py
git commit -m "feat(daemon): get_daemon_status read accessor (2B.2)"
```

---

### Task 5: Daemon startup clear + reconcile heartbeat

**Files:**
- Modify: `src/localmail/daemon.py`
- Create: `tests/test_daemon_heartbeats_wiring.py` (reconcile + startup cases first)

- [ ] **Step 1: Write the failing daemon-wiring tests**

Create `tests/test_daemon_heartbeats_wiring.py`:

```python
"""Heartbeat wiring: reconcile writes a heartbeat; startup clears stale rows."""
from __future__ import annotations

import threading

import psycopg
from psycopg_pool import ConnectionPool

import localmail.daemon as daemon_mod
from localmail.config import LocalmailConfig
from localmail.daemon import Daemon
from localmail.heartbeat import record_heartbeat


def _cfg(db_dsn: str) -> LocalmailConfig:
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    return cfg


def _heartbeat_kinds(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT worker_kind FROM daemon_heartbeats")
            return {r[0] for r in cur.fetchall()}


def test_reconcile_records_reconcile_heartbeat(db_dsn: str) -> None:
    # Clean slate.
    with psycopg.connect(db_dsn) as conn:
        conn.execute("TRUNCATE daemon_heartbeats RESTART IDENTITY CASCADE")
        conn.commit()
    d = Daemon(_cfg(db_dsn), ssl=False, stop_event=threading.Event())
    try:
        d.reconcile()
        assert "reconcile" in _heartbeat_kinds(db_dsn)
    finally:
        d.pool.close()


def test_startup_clears_leftover_heartbeats(db_dsn: str) -> None:
    # Simulate leftover rows from a previous crashed run.
    with psycopg.connect(db_dsn) as conn:
        conn.execute("TRUNCATE daemon_heartbeats RESTART IDENTITY CASCADE")
        record_heartbeat(conn, worker_kind="embed", account_id=None, state="idle")
        conn.commit()
    d = Daemon(_cfg(db_dsn), ssl=False, stop_event=threading.Event())
    try:
        d.start_workers()  # spawns no account threads (no syncable accounts), clears HBs
        assert "embed" not in _heartbeat_kinds(db_dsn)
    finally:
        d.stop()
        d.pool.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_heartbeats_wiring.py -q`
Expected: FAIL — reconcile writes no heartbeat; startup does not clear.

- [ ] **Step 3: Wire startup clear + reconcile heartbeat in `daemon.py`**

In `src/localmail/daemon.py`, add the import near the other local imports (after `from .daemon_reconcile import plan_reconcile`):

```python
from .heartbeat import clear_all_heartbeats, record_heartbeat, safe_heartbeat
```

Add a startup-clear helper and call it in `start_workers`. Replace the body of `start_workers`:

```python
    def start_workers(self) -> None:
        if self._started:
            return
        self._started = True
        self._clear_heartbeats()
        for account_row in self._syncable:
            self._spawn_account(account_row)
        self._spawn_worker_threads()

    def _clear_heartbeats(self) -> None:
        """Single-instance reset: drop any heartbeat rows from a previous run
        so a crashed predecessor's rows never read as live. Best-effort."""
        try:
            with psycopg.connect(self._dsn) as conn:
                clear_all_heartbeats(conn)
                conn.commit()
        except Exception:
            log.warning("startup heartbeat clear failed", exc_info=True)
```

In `reconcile`, record a `reconcile` heartbeat on the same fresh connection right after reading the desired set. Replace the read block at the top of `reconcile`:

```python
        try:
            with psycopg.connect(self._dsn) as conn:
                desired_rows = list_syncable_accounts(conn)
                record_heartbeat(conn, worker_kind="reconcile",
                                 account_id=None, state="idle")
                conn.commit()
        except Exception:
            log.warning(
                "reconcile: failed to read accounts; keeping current threads",
                exc_info=True,
            )
            return
```

(Note: `safe_heartbeat` is imported here for Tasks 6–8; it is unused in this task. To avoid an unused-import lint failure between commits, add the `safe_heartbeat` wiring in Task 6/7/8 in the same branch — or import only `clear_all_heartbeats, record_heartbeat` now and add `safe_heartbeat` to the import line in Task 7. **Choose the latter:** in this task import only `clear_all_heartbeats, record_heartbeat`.)

So the actual import line for THIS task is:

```python
from .heartbeat import clear_all_heartbeats, record_heartbeat
```

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_heartbeats_wiring.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Run mypy on the touched module**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail/daemon.py`
Expected: Success.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/daemon.py tests/test_daemon_heartbeats_wiring.py
git commit -m "feat(daemon): startup heartbeat clear + reconcile heartbeat (2B.2)"
```

---

### Task 6: Wire `idle` + `poll` loop heartbeats

**Files:**
- Modify: `src/localmail/idle.py`
- Modify: `src/localmail/poller.py`
- Modify: `tests/test_daemon_heartbeats_wiring.py` (add idle/poll spy cases)

- [ ] **Step 1: Write the failing idle/poll wiring tests**

Append to `tests/test_daemon_heartbeats_wiring.py`:

```python
import time
from contextlib import contextmanager

import localmail.idle as idle_mod
import localmail.poller as poll_mod
from localmail.idle import _one_inbox_session
from localmail.poller import _one_poll_pass
from localmail.worker import WorkerContext
from localmail.config import AccountConfig
from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account, get_account_by_name
from tests._fake_imap import FakeIMAPClient


class _HBSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(self, pool, *, worker_kind, account_id, state,
                 current_folder=None, last_error_msg=None):  # noqa: ARG002
        self.calls.append((worker_kind, state, current_folder))


def _account_id(pool: ConnectionPool, name: str = "hbacct") -> int:
    cfg = AccountConfig(name=name, email=f"{name}@example.com",
                        imap_host="imap.example.com", imap_port=993,
                        auth_method="password")
    with pool.connection() as conn:
        existing = get_account_by_name(conn, name)
        aid = existing.id if existing else create_account(
            conn, **account_create_kwargs(cfg)).id
        conn.commit()
    return aid


@contextmanager
def _fake_open(imap, account, **kw):  # noqa: ARG001
    yield imap


def _ctx(pool, tmp_path, aid, stop):
    return WorkerContext(
        account=AccountConfig(name="hbacct", email="hbacct@example.com",
                              imap_host="imap.example.com", imap_port=993,
                              auth_method="password"),
        account_id=aid, pool=pool, attachments_root=tmp_path,
        idle_renew_seconds=60, poll_seconds=1, gmail_client_secrets=None,
        stop=stop, ssl=False)


def test_idle_session_records_connecting_then_idle(db_dsn, tmp_path, monkeypatch) -> None:
    pool = ConnectionPool(conninfo=db_dsn, min_size=1, max_size=2, open=True)
    try:
        with pool.connection() as conn:
            conn.execute("TRUNCATE daemon_heartbeats RESTART IDENTITY CASCADE")
            conn.commit()
        aid = _account_id(pool)
        imap = FakeIMAPClient()
        imap.add_folder("INBOX")
        spy = _HBSpy()
        monkeypatch.setattr(idle_mod, "safe_heartbeat", spy)
        monkeypatch.setattr(idle_mod, "open_connection",
                            lambda *a, **k: _fake_open(imap, None))
        stop = threading.Event()
        stop.set()  # exit the idle inner loop immediately after connect+catchup
        _one_inbox_session(_ctx(pool, tmp_path, aid, stop))
        states = [(k, s) for k, s, _ in spy.calls if k == "idle"]
        assert ("idle", "connecting") in states
    finally:
        pool.close()


def test_poll_pass_records_polling_and_syncing(db_dsn, tmp_path, monkeypatch) -> None:
    pool = ConnectionPool(conninfo=db_dsn, min_size=1, max_size=2, open=True)
    try:
        with pool.connection() as conn:
            conn.execute("TRUNCATE daemon_heartbeats RESTART IDENTITY CASCADE")
            conn.commit()
        aid = _account_id(pool)
        imap = FakeIMAPClient.with_folders(["INBOX", "Archive"])
        spy = _HBSpy()
        monkeypatch.setattr(poll_mod, "safe_heartbeat", spy)
        monkeypatch.setattr(poll_mod, "open_connection",
                            lambda *a, **k: _fake_open(imap, None))
        _one_poll_pass(_ctx(pool, tmp_path, aid, threading.Event()))
        kinds_states = [(k, s) for k, s, _ in spy.calls]
        assert ("poll", "polling") in kinds_states
        folders = [cf for k, s, cf in spy.calls if s == "syncing"]
        assert "Archive" in folders
    finally:
        pool.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_heartbeats_wiring.py -q -k "idle_session or poll_pass"`
Expected: FAIL — `idle_mod` / `poll_mod` have no `safe_heartbeat` attribute, and no calls recorded.

- [ ] **Step 3: Wire `idle.py`**

In `src/localmail/idle.py`, add the import after `from .imap_client import open_connection`:

```python
from .heartbeat import safe_heartbeat
```

In `_one_inbox_session`, record `connecting` right after opening, and `idle` once catch-up is done and IDLE is entered. Replace the body:

```python
def _one_inbox_session(ctx: WorkerContext) -> None:
    """One full lifecycle of an IDLE-on-INBOX session. Returns when stop is set
    or when the IDLE call raises (caller retries with backoff)."""
    with open_connection(
        ctx.account,
        ssl=ctx.ssl,
        gmail_client_secrets=ctx.gmail_client_secrets,
    ) as imap:
        safe_heartbeat(ctx.pool, worker_kind="idle",
                       account_id=ctx.account_id, state="connecting")
        account_id, mailbox = _ensure_inbox_row(ctx)
        imap.select_folder(INBOX)

        # Catch up on anything that arrived while the daemon was down.
        _sync_inbox(ctx, imap, account_id)

        imap.idle()
        safe_heartbeat(ctx.pool, worker_kind="idle",
                       account_id=ctx.account_id, state="idle")
        try:
            renew_at = time.monotonic() + ctx.idle_renew_seconds
            while not ctx.stop.is_set():
                renew_at = _idle_step(ctx, imap, account_id, renew_at)
        finally:
            try:
                imap.idle_done()
            except Exception:
                pass
```

In `_idle_step`, record an `idle` heartbeat at the top (this is the ~30s liveness tick) and `syncing` when new mail arrives. Replace the body:

```python
def _idle_step(ctx: WorkerContext, imap: Any, account_id: int, renew_at: float) -> float:
    """Wait briefly for IDLE notifications. If any, sync and re-issue IDLE.
    If the renewal deadline is reached, force-cycle IDLE. Return the next
    renewal deadline (monotonic timestamp)."""
    safe_heartbeat(ctx.pool, worker_kind="idle",
                   account_id=ctx.account_id, state="idle")
    budget = max(1.0, renew_at - time.monotonic())
    timeout = float(min(HEARTBEAT_SECONDS, budget))
    responses = imap.idle_check(timeout=timeout) or []

    if ctx.stop.is_set():
        return renew_at

    if responses:
        imap.idle_done()
        safe_heartbeat(ctx.pool, worker_kind="idle",
                       account_id=ctx.account_id, state="syncing",
                       current_folder=INBOX)
        _sync_inbox(ctx, imap, account_id)
        imap.idle()
        return time.monotonic() + ctx.idle_renew_seconds

    if time.monotonic() >= renew_at - RENEW_GUARD_SECONDS:
        imap.idle_done()
        imap.idle()
        return time.monotonic() + ctx.idle_renew_seconds

    return renew_at
```

In `run_inbox_idle_loop`, record `reconnecting` on the exception path. Replace the body:

```python
def run_inbox_idle_loop(ctx: WorkerContext) -> None:
    """Long-running loop: open IMAP, run an IDLE session, reconnect on failure."""
    backoff = 1.0
    while not ctx.stop.is_set():
        try:
            _one_inbox_session(ctx)
            backoff = 1.0
        except Exception as exc:
            log.exception("inbox-idle session crashed for %s", ctx.account.name)
            safe_heartbeat(ctx.pool, worker_kind="idle",
                           account_id=ctx.account_id, state="reconnecting",
                           last_error_msg=str(exc))
            if ctx.stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)
```

- [ ] **Step 4: Wire `poller.py`**

In `src/localmail/poller.py`, add after `from .imap_client import open_connection`:

```python
from .heartbeat import safe_heartbeat
```

In `_one_poll_pass`, record `polling` after opening. Insert right after the `with open_connection(...) as imap:` line opens and `account_id` is bound:

```python
def _one_poll_pass(ctx: WorkerContext) -> dict[str, int]:
    """Open a connection, sync every non-INBOX folder, close. Returns
    `{folder_name: new_messages}`."""
    results: dict[str, int] = {}
    with open_connection(
        ctx.account,
        ssl=ctx.ssl,
        gmail_client_secrets=ctx.gmail_client_secrets,
    ) as imap:
        account_id = ctx.account_id
        safe_heartbeat(ctx.pool, worker_kind="poll",
                       account_id=account_id, state="polling")

        folders = imap.list_folders()
        selectable = folders_to_sync(
            folders,
            allow=ctx.account.folder_allow,
            deny=ctx.account.folder_deny,
            deny_flags=ctx.account.folder_deny_flags,
        )

        for name, delim, flags in selectable:
            if ctx.stop.is_set():
                break
            if name == INBOX:
                continue  # owned by the IDLE loop
            safe_heartbeat(ctx.pool, worker_kind="poll", account_id=account_id,
                           state="syncing", current_folder=name)
            results[name] = _sync_folder(ctx, imap, account_id, name, delim, flags)

    return results
```

In `run_poll_loop`, record `reconnecting` on the exception path. Replace the body:

```python
def run_poll_loop(ctx: WorkerContext) -> None:
    """Long-running loop: every `poll_seconds`, sync every non-INBOX folder."""
    backoff = 1.0
    while not ctx.stop.is_set():
        try:
            _one_poll_pass(ctx)
            backoff = 1.0
        except Exception as exc:
            log.exception("poll pass crashed for %s", ctx.account.name)
            safe_heartbeat(ctx.pool, worker_kind="poll",
                           account_id=ctx.account_id, state="reconnecting",
                           last_error_msg=str(exc))
            if ctx.stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)
            continue
        if ctx.stop.wait(ctx.poll_seconds):
            break
```

- [ ] **Step 5: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_heartbeats_wiring.py -q`
Expected: PASS (all wiring tests).

- [ ] **Step 6: Run the existing daemon tests (no regressions)**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon.py tests/test_daemon_hot_reload.py -q`
Expected: PASS (heartbeats route through `safe_heartbeat`, which swallows; existing fakes still drive sync correctly).

- [ ] **Step 7: Commit**

```bash
git add src/localmail/idle.py src/localmail/poller.py tests/test_daemon_heartbeats_wiring.py
git commit -m "feat(daemon): idle + poll loop heartbeats (2B.2)"
```

---

### Task 7: Wire `embed` + `extract` process-worker heartbeats

**Files:**
- Modify: `src/localmail/search/embed_worker.py`
- Modify: `src/localmail/search/extract_worker.py`
- Modify: `tests/test_daemon_heartbeats_wiring.py` (add embed/extract spy cases)

- [ ] **Step 1: Write the failing embed/extract wiring tests**

Append to `tests/test_daemon_heartbeats_wiring.py`:

```python
import localmail.search.embed_worker as embed_mod
import localmail.search.extract_worker as extract_mod
from localmail.config import SearchConfig


class _ProcHBSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, pool, *, worker_kind, account_id, state,
                 current_folder=None, last_error_msg=None):  # noqa: ARG002
        assert account_id is None  # process-level workers are account-agnostic
        self.calls.append((worker_kind, state))


def test_embed_worker_records_embed_heartbeat(db_dsn, monkeypatch) -> None:
    pool = ConnectionPool(conninfo=db_dsn, min_size=1, max_size=2, open=True)
    try:
        spy = _ProcHBSpy()
        monkeypatch.setattr(embed_mod, "safe_heartbeat", spy)
        # Make a single sweep a no-op that returns 0 written.
        monkeypatch.setattr(embed_mod, "run_embed_worker_once",
                            lambda *a, **k: 0)
        stop = threading.Event()

        class _Backend:
            name = "fake"; model = "fake"; dimension = 768
            def embed_documents(self, t): return [[0.0] * 768 for _ in t]
            def embed_query(self, t): return [0.0] * 768
            def health_check(self): pass

        # Run the loop briefly in a thread, then stop.
        cfg = SearchConfig(embed_worker_poll_interval_s=30)
        th = threading.Thread(
            target=embed_mod.run_embed_worker,
            args=(stop, pool, cfg, _Backend()), daemon=True)
        th.start()
        time.sleep(0.2)
        stop.set()
        th.join(timeout=5)
        assert ("embed", "idle") in spy.calls
    finally:
        pool.close()


def test_extract_worker_records_extract_heartbeat(db_dsn, monkeypatch) -> None:
    pool = ConnectionPool(conninfo=db_dsn, min_size=1, max_size=2, open=True)
    try:
        spy = _ProcHBSpy()
        monkeypatch.setattr(extract_mod, "safe_heartbeat", spy)
        monkeypatch.setattr(extract_mod, "run_extract_worker_once",
                            lambda *a, **k: 0)
        stop = threading.Event()
        cfg = SearchConfig(extract_worker_poll_interval_s=30)
        th = threading.Thread(
            target=extract_mod.run_extract_worker,
            kwargs={"pool": pool, "cfg": cfg, "stop_event": stop}, daemon=True)
        th.start()
        time.sleep(0.2)
        stop.set()
        th.join(timeout=5)
        assert ("extract", "idle") in spy.calls
    finally:
        pool.close()
```

> Note: confirm the exact name of the extract per-sweep function (`run_extract_worker_once` per CLAUDE.md). If it differs, adjust the monkeypatch target in the test and the call site in Step 3 to match. Check with `grep -n "def run_extract_worker_once" src/localmail/search/extract_worker.py`.

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_heartbeats_wiring.py -q -k "embed_worker_records or extract_worker_records"`
Expected: FAIL — modules have no `safe_heartbeat`; no calls recorded.

- [ ] **Step 3: Wire `embed_worker.py`**

In `src/localmail/search/embed_worker.py`, add the import (top-level, with the other `localmail` imports):

```python
from localmail.heartbeat import safe_heartbeat
```

In `run_embed_worker`, record an `embed` heartbeat at the top of each sweep (liveness ping) and `error` on a sweep exception. Modify the loop body:

```python
    consecutive_empty = 0
    while not stop.is_set():
        safe_heartbeat(pool, worker_kind="embed", account_id=None, state="idle")
        try:
            with pool.connection() as conn:
                wrote = run_embed_worker_once(
                    conn, cfg, backend, lang_detector=lang_detector,
                )
        except Exception as exc:  # noqa: BLE001
            log.error("embed_worker sweep error: %s", exc, exc_info=True)
            safe_heartbeat(pool, worker_kind="embed", account_id=None,
                           state="error", last_error_msg=str(exc))
            wrote = 0
        if wrote == 0:
            consecutive_empty = min(consecutive_empty + 1, 6)
        else:
            consecutive_empty = 0
        sleep_s = cfg.embed_worker_poll_interval_s * (1 + consecutive_empty)
        stop.wait(timeout=sleep_s)
```

- [ ] **Step 4: Wire `extract_worker.py`**

In `src/localmail/search/extract_worker.py`, add the import (top-level):

```python
from localmail.heartbeat import safe_heartbeat
```

In `run_extract_worker`, record an `extract` heartbeat at the top of each sweep. Insert at the top of the `while not stop_event.is_set():` loop body (before the `try:`):

```python
    backoff = _INITIAL_BACKOFF_S
    while not stop_event.is_set():
        safe_heartbeat(pool, worker_kind="extract", account_id=None, state="idle")
        try:
            with pool.connection() as conn:
                while not stop_event.is_set():
                    ...
```

And on the pool-exception path in that loop (the `except` that triggers backoff), add:

```python
            safe_heartbeat(pool, worker_kind="extract", account_id=None,
                           state="error", last_error_msg=str(exc))
```

> Read the existing `run_extract_worker` body (lines ~505+) to place the `state="error"` call inside the existing `except` block, binding the exception to `exc` if it is not already (`except Exception as exc:`). Keep the existing backoff/log logic intact.

- [ ] **Step 5: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_heartbeats_wiring.py -q`
Expected: PASS (all wiring tests).

- [ ] **Step 6: Run the existing embed/extract worker tests (no regressions)**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_embed_worker.py tests/test_extract_worker.py tests/test_daemon_embed_thread.py tests/test_daemon_extract_thread.py -q`
Expected: PASS — the `_once` functions are untouched; loop-level tests still drain/stop as before (heartbeat writes go to the real test DB, which now has the table).

- [ ] **Step 7: Commit**

```bash
git add src/localmail/search/embed_worker.py src/localmail/search/extract_worker.py tests/test_daemon_heartbeats_wiring.py
git commit -m "feat(daemon): embed + extract worker heartbeats (2B.2)"
```

---

### Task 8: Docs — config.example.toml + README + full-suite gate

**Files:**
- Modify: `config.example.toml`
- Modify: `README.md`

- [ ] **Step 1: Document the knob in `config.example.toml`**

Find the `[daemon]` section (where `reload_seconds` / `shutdown_grace_seconds` were added in 2B.1) and add:

```toml
# A worker's heartbeat is considered "stale" when now() - last_heartbeat_at
# exceeds this many seconds. The daemon records per-thread liveness in the
# daemon_heartbeats table; an operator reads it (admin daemon-status, 2B.4).
# Default comfortably exceeds the ~30s IDLE heartbeat tick.
heartbeat_stale_seconds = 120
```

- [ ] **Step 2: One-line note in `README.md`**

In the section describing `localmail run` (the daemon), add a sentence noting that the daemon records per-thread liveness heartbeats in `daemon_heartbeats` (account IDLE + poll threads, plus embed/extract/reconcile process workers), with staleness governed by `daemon.heartbeat_stale_seconds`. Match the surrounding prose style; do not restate the schema.

- [ ] **Step 3: Run the FULL suite + mypy (the slice gate)**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/`
Expected: PASS — baseline 1081 + the new heartbeat tests (≈ +20). No failures.

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: `Success: no issues found in N source files` (N = 76 + new modules).

- [ ] **Step 4: Commit**

```bash
git add config.example.toml README.md
git commit -m "docs(daemon): document heartbeat_stale_seconds + README heartbeat note (2B.2)"
```

---

## Self-review checklist (run after the plan is written, before execution)

1. **Spec coverage (§2B.2):** migration ✓ (Task 1), writer `record_heartbeat` ✓ (Task 2), both partial-index conflict targets ✓ (Task 2 account + process cases), single-instance startup DELETE ✓ (Task 5 `_clear_heartbeats`), reader `get_daemon_status` + staleness ✓ (Task 4), new knob `heartbeat_stale_seconds` ✓ (Task 3), loops call the writer (idle/poll ✓ Task 6, embed/extract ✓ Task 7, reconcile ✓ Task 5). **No HTTP route / CLI** — correctly deferred to 2B.4 (slice table: "read accessor only").
2. **Placeholder scan:** every code step shows complete code; the two "read the existing body to place the call" notes (Task 7 Step 4) point at a specific existing `except` block and are accompanied by the exact lines to insert — not "add error handling".
3. **Type/name consistency:** `record_heartbeat` / `clear_all_heartbeats` / `safe_heartbeat` signatures identical across Tasks 2, 5, 6, 7. `WorkerKind` / `WorkerState` Literals match the SQL CHECK constraint (Task 1) exactly. `HeartbeatRow` field names match the `_SELECT` columns in `get_daemon_status` (Task 4) for `class_row` name-mapping. `daemon.heartbeat_stale_seconds` (Task 3) is the arg passed to `get_daemon_status(stale_seconds=…)` (Task 4) — consumer wiring is 2B.4, not this slice.

## Risks / notes for the executor

- **Import-ordering between commits (Task 5 vs 6/7):** Task 5 imports only `clear_all_heartbeats, record_heartbeat` in `daemon.py`; `safe_heartbeat` is imported in `idle.py`/`poller.py` (Task 6) and the worker modules (Task 7), NOT in `daemon.py`. This keeps every intermediate commit lint-clean.
- **Heartbeat writes use the shared pool** (`safe_heartbeat(ctx.pool, …)`) for idle/poll/embed/extract, and a fresh `psycopg.connect` for reconcile/startup-clear (matching the existing reconcile pattern). The pool was sized in 2B.1 for the long-lived workers; a heartbeat borrow is sub-ms and released immediately, so it does not change pool-sizing assumptions. Do NOT add a dedicated heartbeat connection.
- **`safe_heartbeat` never raises** — verified by `test_safe_heartbeat_swallows_pool_errors` (Task 2). This is load-bearing: a heartbeat-write failure must never crash a sync loop.
- **`started_at` is frozen on insert** (`DO UPDATE` omits it) — pinned by `test_started_at_is_preserved_across_updates` (Task 2).
- The wiring tests spy on `safe_heartbeat` (monkeypatch) so they assert the loop's *intent* (kind + state + folder) without depending on real heartbeat-row contents; the real DB round-trip is covered by Task 2 (writer) and Task 4 (reader).
