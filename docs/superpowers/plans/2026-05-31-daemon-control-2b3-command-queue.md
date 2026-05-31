# Daemon Control 2B.3 — Command Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `daemon_commands` queue (Plane A) so operators can issue imperative actions the desired-state reconcile can't express — `reload-now`, `restart-account`, `drain-stop` — consumed by the running daemon at the top of each reconcile tick, with a `LISTEN/NOTIFY` low-latency wake so `reload-now` converges immediately instead of waiting out `reload_seconds`.

**Architecture:** A new migration adds the `daemon_commands` table. A service-layer module (`api/admin/daemon.py`, alongside the existing 2B.2 `get_daemon_status`) gains an `enqueue_command` accessor (the only write surface this slice ships — HTTP/CLI is 2B.4) plus consumer helpers `claim_commands` / `mark_command`. The `Daemon` drains the queue at the top of `reconcile()` via `FOR UPDATE SKIP LOCKED`, applies each command against its live thread registry, and marks it `done`/`failed`. A dedicated short-lived listener thread `LISTEN`s the `daemon_commands` channel and sets a `threading.Event` that the reconcile loop also waits on, so an enqueue's `NOTIFY` wakes the loop early. The poll path (`reload_seconds`) remains authoritative and correct on its own; `NOTIFY` only reduces latency.

**Tech Stack:** Python ≥3.12, psycopg v3 (3.3.4 — `Connection.notifies(timeout=, stop_after=)`), raw SQL + numbered migrations, pydantic v2 config, pytest. No ORM. No magic numbers (timing knobs live on `DaemonConfig`).

---

## Context the worker needs

- **Spec:** `docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md` §2B.3 (command queue) and the cross-cutting decisions. Read §2B.3 and decisions 2 + 3 before starting.
- **Plane A vs B:** This slice is pure Plane A (DB-mediated, supervisor-agnostic). It ships **no** HTTP route and **no** CLI — only the DB table, the service-layer `enqueue_command` accessor, and the daemon consumer. The HTTP/CLI surface is 2B.4.
- **Command queue ≠ account state** (spec decision 2): add/remove/pause/resume stay as `accounts` edits the reconcile already picks up. The queue is only for the three imperative actions.
- **Single daemon instance** (spec decision 3): `FOR UPDATE SKIP LOCKED` is defensive, not a clustering claim.
- **Existing patterns to mirror:**
  - Migration shape: `migrations/0023_daemon_heartbeats.sql` (CHECK constraints, partial indexes, leading comment block).
  - Service-layer accessor + dataclass: `src/localmail/api/admin/daemon.py` (`get_daemon_status`, `HeartbeatRow` — `class_row` mapping, no commit, no FastAPI).
  - Daemon thread registry + reconcile: `src/localmail/daemon.py` (`reconcile`, `_teardown_account`, `_spawn_account`, `run_forever`, `_connect`).
  - Reconcile/hot-reload tests: `tests/test_daemon_hot_reload.py` (the `quiet_threads` fixture, `_row`, `_make_daemon`, `_cfg`).
  - Heartbeat/service DB tests: `tests/test_heartbeat.py`, `tests/test_admin_daemon.py` (the `_account` helper via `create_account` + `account_create_kwargs`).
- **`_connect()` caveat:** the daemon's fresh-connect helper applies `statement_timeout=<N>s` via `options=`. A long-lived `LISTEN` connection must NOT keep that bound (it would be irrelevant during a socket wait, but disable it explicitly for clarity) and must be `autocommit=True` so `LISTEN` takes effect and notifications are delivered promptly.
- **NOTIFY is transactional:** `NOTIFY daemon_commands` issued inside `enqueue_command` is delivered to listeners only when the caller commits. The channel name is a SQL identifier literal (not parameterizable) — hardcode `daemon_commands`.

## File structure

```
migrations/0024_daemon_commands.sql        # NEW — daemon_commands table + queue index
src/localmail/config.py                     # +command_listen_enabled, +command_listen_poll_seconds on DaemonConfig
src/localmail/api/admin/daemon.py           # +DaemonCommand, +enqueue_command, +claim_commands, +mark_command
src/localmail/daemon.py                     # +_reconcile_wake, _drain_commands/_apply_command, reconcile wiring, listener thread, wake loop
config.example.toml                         # [daemon] command_listen_* knobs
tests/conftest.py                           # +daemon_commands in TRUNCATE list
tests/test_daemon_commands_service.py       # NEW — migration CHECKs, enqueue+NOTIFY, claim SKIP LOCKED, mark
tests/test_daemon_command_consume.py        # NEW — _drain_commands/_apply_command effects against a real-DB daemon
tests/test_daemon_command_listen.py         # NEW — listener sets wake on NOTIFY; run_forever reconciles early
tests/test_config.py                        # +command_listen_* default/override round-trip
README.md                                   # run-row clause: command queue
ROADMAP.md                                  # mark 2B.3 done
```

---

## Task 1: Migration `0024_daemon_commands.sql` + conftest TRUNCATE

**Files:**
- Create: `migrations/0024_daemon_commands.sql`
- Modify: `tests/conftest.py:73-81` (TRUNCATE list)
- Test: `tests/test_daemon_commands_service.py` (migration CHECK constraints — written in Task 3; this task is verified by the migration applying cleanly via the `db_dsn` session fixture)

- [ ] **Step 1: Confirm the next migration slot is free**

Run: `ls migrations/ | tail -3`
Expected: latest is `0023_daemon_heartbeats.sql`; no `0024_*`. (If another `0024` landed first, renumber this file to the next free slot and update every reference in this plan.)

- [ ] **Step 2: Write the migration**

Create `migrations/0024_daemon_commands.sql`:

```sql
-- Daemon command queue (2B.3, Plane A — DB-mediated, supervisor-agnostic).
-- Carries imperative actions that are NOT expressible as desired account state:
--   reload-now       force an immediate reconcile (don't wait out reload_seconds)
--   restart-account  teardown + respawn one account's thread bundle (account_id required)
--   drain-stop       set the master stop event; the daemon drains and exits
-- Add/remove/pause/resume an account stay as `accounts` edits the reconcile picks
-- up — they are NOT commands (see the 2B re-spec, decision 2). The daemon drains
-- this queue at the top of each reconcile tick (FOR UPDATE SKIP LOCKED) and an
-- enqueue NOTIFYs the `daemon_commands` channel so a listening daemon wakes early.
-- Single-instance daemon is assumed (multi-host is a non-goal); SKIP LOCKED is
-- defensive, not a clustering claim.

CREATE TABLE daemon_commands (
    id           BIGSERIAL    PRIMARY KEY,
    command      TEXT         NOT NULL
                              CHECK (command IN
                                     ('reload-now','restart-account','drain-stop')),
    -- required iff restart-account; forbidden otherwise (enforced by the CHECK below)
    account_id   BIGINT       REFERENCES accounts(id) ON DELETE CASCADE,
    state        TEXT         NOT NULL DEFAULT 'queued'
                              CHECK (state IN ('queued','done','failed')),
    requested_by INT          REFERENCES api_users(id),
    requested_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    picked_at    TIMESTAMPTZ,
    done_at      TIMESTAMPTZ,
    result_msg   TEXT,
    CHECK ((command = 'restart-account') = (account_id IS NOT NULL))
);

-- Partial index over only the queued rows the consumer scans, oldest first.
CREATE INDEX daemon_commands_queue_idx
    ON daemon_commands (requested_at) WHERE state = 'queued';
```

- [ ] **Step 3: Add `daemon_commands` to the conftest TRUNCATE list**

In `tests/conftest.py`, the `db_conn` fixture's TRUNCATE (currently ends `"daemon_heartbeats "`). Add `daemon_commands` (before `daemon_heartbeats` so it's truncated; CASCADE + RESTART IDENTITY already in the statement):

```python
                "api_users, api_tokens, user_accounts, api_login_attempts, "
                "daemon_commands, daemon_heartbeats "
                "RESTART IDENTITY CASCADE"
```

- [ ] **Step 4: Verify the migration applies cleanly**

Run: `unset VIRTUAL_ENV && uv run python -c "from tests.conftest import TEST_DSN; from localmail.db import apply_migrations; apply_migrations(TEST_DSN); print('ok')"`
Expected: prints `ok` (no error). If `tests.conftest` import fails standalone, instead run any DB test — e.g. `unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_daemon.py` — the session `db_dsn` fixture calls `apply_migrations` and a green run proves 0024 applied.

- [ ] **Step 5: Commit**

```bash
git add migrations/0024_daemon_commands.sql tests/conftest.py
git commit -m "feat(daemon): add daemon_commands queue table (2B.3 migration)"
```

---

## Task 2: Config knobs for the command listener

**Files:**
- Modify: `src/localmail/config.py` (`DaemonConfig`, after `db_tcp_user_timeout_ms` at line ~95)
- Modify: `config.example.toml` (`[daemon]` section)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing config tests**

Add to `tests/test_config.py`:

```python
def test_daemon_command_listen_defaults():
    from localmail.config import LocalmailConfig

    cfg = LocalmailConfig.model_validate({"database": {"dsn": "postgresql:///x"}})
    assert cfg.daemon.command_listen_enabled is True
    assert cfg.daemon.command_listen_poll_seconds == 5.0


def test_daemon_command_listen_override():
    from localmail.config import LocalmailConfig

    cfg = LocalmailConfig.model_validate(
        {
            "database": {"dsn": "postgresql:///x"},
            "daemon": {
                "command_listen_enabled": False,
                "command_listen_poll_seconds": 2.5,
            },
        }
    )
    assert cfg.daemon.command_listen_enabled is False
    assert cfg.daemon.command_listen_poll_seconds == 2.5
```

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_config.py -k command_listen`
Expected: FAIL — `AttributeError`/validation error (fields don't exist yet).

- [ ] **Step 3: Add the fields to `DaemonConfig`**

In `src/localmail/config.py`, append to `DaemonConfig` immediately after the `db_tcp_user_timeout_ms` field:

```python
    # 2B.3 command queue: the daemon LISTENs the `daemon_commands` channel on a
    # dedicated connection so an enqueue's NOTIFY wakes the reconcile loop early
    # (reload-now converges immediately instead of waiting out reload_seconds).
    # The poll path (reload_seconds) is authoritative and correct on its own;
    # the listener is a pure latency optimization. Disable it where LISTEN is
    # undesirable — the daemon then still consumes commands on the next tick.
    command_listen_enabled: bool = True
    # How long the listener blocks in notifies() before re-checking the stop
    # event (seconds). Bounds shutdown latency of the listener thread; small so
    # a stopping daemon's listener exits promptly, large enough not to busy-spin.
    command_listen_poll_seconds: float = 5.0
```

- [ ] **Step 4: Run to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_config.py -k command_listen`
Expected: PASS (2 tests).

- [ ] **Step 5: Document the knobs in `config.example.toml`**

In `config.example.toml`, under the `[daemon]` section, add (match the surrounding comment style):

```toml
# 2B.3 command queue: LISTEN the daemon_commands channel so an enqueued
# reload-now / restart-account / drain-stop wakes the reconcile loop early
# instead of waiting out reload_seconds. The poll path is authoritative; this
# is a latency optimization. Set command_listen_enabled = false to disable.
# command_listen_enabled = true
# command_listen_poll_seconds = 5.0
```

- [ ] **Step 6: Commit**

```bash
git add src/localmail/config.py config.example.toml tests/test_config.py
git commit -m "feat(daemon): add command_listen config knobs (2B.3)"
```

---

## Task 3: Service layer — `enqueue_command`, `claim_commands`, `mark_command`

**Files:**
- Modify: `src/localmail/api/admin/daemon.py` (add `DaemonCommand` + three functions)
- Test: `tests/test_daemon_commands_service.py` (NEW)

- [ ] **Step 1: Write the failing service tests**

Create `tests/test_daemon_commands_service.py`:

```python
"""daemon_commands service layer: enqueue (+NOTIFY), claim (SKIP LOCKED), mark;
plus the migration's CHECK constraints."""
from __future__ import annotations

import psycopg
import pytest

from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account
from localmail.api.admin.daemon import (
    claim_commands,
    enqueue_command,
    mark_command,
)
from localmail.config import AccountConfig


def _account(conn: psycopg.Connection, name: str = "acct") -> int:
    cfg = AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993, auth_method="password",
    )
    return create_account(conn, **account_create_kwargs(cfg)).id


def _states(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT command, account_id, state FROM daemon_commands ORDER BY id"
        )
        return cur.fetchall()


def test_enqueue_reload_now_returns_id_and_queues(db_conn: psycopg.Connection) -> None:
    cmd_id = enqueue_command(db_conn, command="reload-now", requested_by=None)
    db_conn.commit()
    assert isinstance(cmd_id, int)
    assert _states(db_conn) == [("reload-now", None, "queued")]


def test_enqueue_restart_account_carries_account_id(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    enqueue_command(db_conn, command="restart-account", account_id=aid, requested_by=None)
    db_conn.commit()
    assert _states(db_conn) == [("restart-account", aid, "queued")]


def test_restart_account_requires_account_id(db_conn: psycopg.Connection) -> None:
    # The DB CHECK enforces (command='restart-account') = (account_id IS NOT NULL).
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO daemon_commands (command, account_id) VALUES ('restart-account', NULL)"
        )
    db_conn.rollback()


def test_non_restart_command_forbids_account_id(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO daemon_commands (command, account_id) VALUES ('reload-now', %s)",
            (aid,),
        )
    db_conn.rollback()


def test_enqueue_emits_notify(db_conn: psycopg.Connection, db_dsn: str) -> None:
    listener = psycopg.connect(db_dsn, autocommit=True)
    try:
        listener.execute("LISTEN daemon_commands")
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()  # NOTIFY is delivered on the enqueuer's COMMIT
        got = next(listener.notifies(timeout=5, stop_after=1), None)
        assert got is not None
        assert got.channel == "daemon_commands"
    finally:
        listener.close()


def test_claim_returns_queued_oldest_first_then_mark_done(db_conn: psycopg.Connection) -> None:
    first = enqueue_command(db_conn, command="reload-now", requested_by=None)
    second = enqueue_command(db_conn, command="drain-stop", requested_by=None)
    db_conn.commit()
    claimed = claim_commands(db_conn)
    assert [c.id for c in claimed] == [first, second]
    assert claimed[0].command == "reload-now"
    for c in claimed:
        mark_command(db_conn, c.id, state="done", result_msg="ok")
    db_conn.commit()
    assert {s for _, _, s in _states(db_conn)} == {"done"}


def test_claim_skips_rows_locked_by_another_tx(db_conn: psycopg.Connection, db_dsn: str) -> None:
    enqueue_command(db_conn, command="reload-now", requested_by=None)
    db_conn.commit()
    # A second connection claims (and holds the FOR UPDATE lock) without committing.
    holder = psycopg.connect(db_dsn, autocommit=False)
    try:
        held = claim_commands(holder)
        assert len(held) == 1  # holder grabbed the only queued row
        # Our connection, concurrently, sees nothing (SKIP LOCKED).
        assert claim_commands(db_conn) == []
    finally:
        holder.rollback()
        holder.close()


def test_mark_failed_records_result(db_conn: psycopg.Connection) -> None:
    cid = enqueue_command(db_conn, command="reload-now", requested_by=None)
    db_conn.commit()
    mark_command(db_conn, cid, state="failed", result_msg="boom")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT state, result_msg, done_at FROM daemon_commands WHERE id = %s",
            (cid,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "failed"
        assert row[1] == "boom"
        assert row[2] is not None  # done_at set on terminal mark
```

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_commands_service.py`
Expected: FAIL — `ImportError` for `enqueue_command` / `claim_commands` / `mark_command`.

- [ ] **Step 3: Implement the service functions**

Append to `src/localmail/api/admin/daemon.py` (after `get_daemon_status`). Add `from typing import Literal` to the imports at the top and `from psycopg.rows import class_row` is already imported:

```python
CommandName = Literal["reload-now", "restart-account", "drain-stop"]
CommandState = Literal["queued", "done", "failed"]


@dataclass(frozen=True)
class DaemonCommand:
    id: int
    command: str
    account_id: int | None
    requested_by: int | None
    requested_at: datetime


def enqueue_command(
    conn: psycopg.Connection,
    *,
    command: CommandName,
    account_id: int | None = None,
    requested_by: int | None,
) -> int:
    """Queue one imperative daemon command and NOTIFY the listener. Returns the
    new row id. Does NOT commit (caller owns the tx). The NOTIFY is transactional
    — it reaches a LISTENing daemon only when the caller commits.

    `restart-account` requires `account_id`; the other commands forbid it. The
    DB CHECK is the authority (a bad pairing raises CheckViolation on flush)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO daemon_commands (command, account_id, requested_by) "
            "VALUES (%s, %s, %s) RETURNING id",
            (command, account_id, requested_by),
        )
        row = cur.fetchone()
        assert row is not None
        new_id = int(row[0])
    # Channel name is a SQL identifier literal, not a parameter — hardcoded.
    conn.execute("NOTIFY daemon_commands")
    return new_id


def claim_commands(conn: psycopg.Connection) -> list[DaemonCommand]:
    """Claim every queued command oldest-first, locking the rows so a concurrent
    consumer skips them (FOR UPDATE SKIP LOCKED). Sets picked_at = now() on each.
    Does NOT commit — the caller acts on each command, marks it, then commits so
    the lock is held across the work (single-instance daemon; defensive lock)."""
    with conn.cursor(row_factory=class_row(DaemonCommand)) as cur:
        cur.execute(
            "SELECT id, command, account_id, requested_by, requested_at "
            "FROM daemon_commands WHERE state = 'queued' "
            "ORDER BY requested_at, id FOR UPDATE SKIP LOCKED"
        )
        claimed = cur.fetchall()
    if claimed:
        conn.execute(
            "UPDATE daemon_commands SET picked_at = now() WHERE id = ANY(%s)",
            ([c.id for c in claimed],),
        )
    return claimed


def mark_command(
    conn: psycopg.Connection,
    command_id: int,
    *,
    state: CommandState,
    result_msg: str | None = None,
) -> None:
    """Mark a claimed command terminal (done/failed) with a result message and
    done_at = now(). Does NOT commit (caller owns the tx)."""
    conn.execute(
        "UPDATE daemon_commands SET state = %s, result_msg = %s, done_at = now() "
        "WHERE id = %s",
        (state, result_msg, command_id),
    )
```

`DaemonCommand` uses psycopg `class_row` name-based mapping (same as `HeartbeatRow`); the SELECT column list must match the dataclass field names exactly.

- [ ] **Step 4: Run to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_commands_service.py`
Expected: PASS (8 tests).

- [ ] **Step 5: mypy**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail/api/admin/daemon.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/api/admin/daemon.py tests/test_daemon_commands_service.py
git commit -m "feat(daemon): enqueue/claim/mark daemon_commands service layer (2B.3)"
```

---

## Task 4: Daemon command consumption — `_drain_commands` + `_apply_command`

**Files:**
- Modify: `src/localmail/daemon.py` (imports; new `_drain_commands`/`_apply_command`; wire into `reconcile`)
- Test: `tests/test_daemon_command_consume.py` (NEW)

- [ ] **Step 1: Write the failing consumption tests**

Create `tests/test_daemon_command_consume.py`:

```python
"""Daemon consumes daemon_commands at the top of reconcile (2B.3).

These are real-DB tests: commands FK accounts(id), so accounts must exist in the
DB and the daemon reads them via the real list_syncable_accounts. IDLE/poll loops
are replaced by quiet stubs (the quiet_threads fixture)."""
from __future__ import annotations

import psycopg
import pytest

import localmail.daemon as daemon_mod
from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account
from localmail.api.admin.daemon import enqueue_command
from localmail.config import AccountConfig, LocalmailConfig
from localmail.daemon import Daemon


class _FakeBackend:
    name = "fake"; model = "fake"; dimension = 768
    def embed_documents(self, texts): return [[0.5] * 768 for _ in texts]
    def embed_query(self, _t): return [0.5] * 768
    def health_check(self) -> None: pass


def _cfg(db_dsn):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    cfg.daemon.command_listen_enabled = False  # no real listener in these tests
    return cfg


def _account(conn: psycopg.Connection, name: str) -> int:
    cfg = AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993, auth_method="password",
    )
    return create_account(conn, **account_create_kwargs(cfg)).id


@pytest.fixture
def quiet_threads(monkeypatch):
    monkeypatch.setattr(daemon_mod, "run_inbox_idle_loop", lambda ctx: ctx.stop.wait())
    monkeypatch.setattr(daemon_mod, "run_poll_loop", lambda ctx: ctx.stop.wait())


def _make_daemon(db_dsn):
    return Daemon(cfg=_cfg(db_dsn), dsn=db_dsn,
                  embedding_backend_factory=lambda c: _FakeBackend())


def _command_states(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT command, state FROM daemon_commands ORDER BY id")
        return cur.fetchall()


def test_reload_now_command_marked_done(db_conn, db_dsn, quiet_threads):
    _account(db_conn, "a1"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()
        d.reconcile()
        assert _command_states(db_conn) == [("reload-now", "done")]
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()


def test_restart_account_tears_down_and_respawns(db_conn, db_dsn, quiet_threads):
    aid = _account(db_conn, "a1"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        old = d._account_threads[aid]
        enqueue_command(db_conn, command="restart-account", account_id=aid,
                        requested_by=None)
        db_conn.commit()
        d.reconcile()  # drain tears aid down; the same-tick diff respawns it
        assert aid in d._account_threads
        assert d._account_threads[aid] is not old
        assert old.stop_event.is_set()
        assert _command_states(db_conn) == [("restart-account", "done")]
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()


def test_restart_account_leaves_other_accounts_untouched(db_conn, db_dsn, quiet_threads):
    a1 = _account(db_conn, "a1"); a2 = _account(db_conn, "a2"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        other = d._account_threads[a2]
        enqueue_command(db_conn, command="restart-account", account_id=a1,
                        requested_by=None)
        db_conn.commit()
        d.reconcile()
        assert d._account_threads[a2] is other  # untouched
        assert not other.stop_event.is_set()
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()


def test_drain_stop_sets_master_stop_event(db_conn, db_dsn, quiet_threads):
    _account(db_conn, "a1"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        enqueue_command(db_conn, command="drain-stop", requested_by=None)
        db_conn.commit()
        d.reconcile()
        assert d._stop_event.is_set()
        assert _command_states(db_conn) == [("drain-stop", "done")]
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()


def test_drain_command_failure_marks_failed_and_survives(db_conn, db_dsn, quiet_threads,
                                                          monkeypatch):
    _account(db_conn, "a1"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()

        def boom(cmd):
            raise RuntimeError("apply failed")

        monkeypatch.setattr(d, "_apply_command", boom)
        d.reconcile()  # must not raise
        assert _command_states(db_conn) == [("reload-now", "failed")]
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_command_consume.py`
Expected: FAIL — reconcile does not yet drain commands; `reload-now` stays `queued`, `_apply_command` doesn't exist.

- [ ] **Step 3: Implement consumption in `daemon.py`**

In `src/localmail/daemon.py`, extend the service-layer import (currently `from .api.admin.accounts import Account, list_syncable_accounts`) — add the command accessors:

```python
from .api.admin.daemon import (
    DaemonCommand,
    claim_commands,
    mark_command,
)
```

Add two methods to `Daemon` (place them just above `reconcile`):

```python
    def _drain_commands(self) -> None:
        """Claim and apply every queued daemon command, marking each done/failed.

        Runs at the top of each reconcile tick on a fresh bounded connection. The
        FOR UPDATE lock is held across apply+mark until the single commit, so a
        concurrent consumer (defensive — single daemon assumed) skips claimed
        rows. A drain failure is logged and swallowed: existing threads keep
        running and the next tick retries."""
        try:
            with self._connect() as conn:
                commands = claim_commands(conn)
                for cmd in commands:
                    try:
                        msg = self._apply_command(cmd)
                        mark_command(conn, cmd.id, state="done", result_msg=msg)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("daemon command %s (id=%s) failed",
                                    cmd.command, cmd.id, exc_info=True)
                        mark_command(conn, cmd.id, state="failed",
                                     result_msg=str(exc))
                conn.commit()
        except Exception:
            log.warning("command drain failed; will retry next tick", exc_info=True)

    def _apply_command(self, cmd: DaemonCommand) -> str:
        """Apply one command against the live thread registry; return a result
        message. `restart-account` only tears the bundle down — the same-tick
        reconcile diff respawns it if the account is still syncable (running set
        now lacks it; desired set still has it)."""
        if cmd.command == "reload-now":
            return "reconcile triggered"
        if cmd.command == "drain-stop":
            self._stop_event.set()
            self._reconcile_wake.set()
            return "daemon stopping"
        if cmd.command == "restart-account":
            assert cmd.account_id is not None  # DB CHECK guarantees this
            self._teardown_account(cmd.account_id)
            return f"account {cmd.account_id} torn down for restart"
        raise ValueError(f"unknown daemon command {cmd.command!r}")
```

Add `self._reconcile_wake = threading.Event()` in `__init__` (Task 5 also uses it; introduce it here so `_apply_command` references a real attribute). Place it next to `self._stop_event` assignment — after line `self._stop_event = stop_event or threading.Event()`:

```python
        self._stop_event = stop_event or threading.Event()
        self._reconcile_wake = threading.Event()
```

Wire the drain into `reconcile` — insert at the very top of the method, before the account read:

```python
    def reconcile(self) -> None:
        """..."""  # keep the existing docstring
        self._drain_commands()
        if self._stop_event.is_set():
            return  # drain-stop fired; run_forever handles shutdown
        try:
            with self._connect() as conn:
                desired_rows = list_syncable_accounts(conn)
        # ... rest unchanged ...
```

- [ ] **Step 4: Run to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_command_consume.py`
Expected: PASS (5 tests).

- [ ] **Step 5: Regression — existing daemon tests still green**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_hot_reload.py tests/test_daemon.py`
Expected: PASS (reconcile now drains first, but with no queued commands the drain is a no-op SELECT; account diff behaviour is unchanged).

- [ ] **Step 6: mypy + commit**

```bash
unset VIRTUAL_ENV && uv run mypy src/localmail/daemon.py
git add src/localmail/daemon.py tests/test_daemon_command_consume.py
git commit -m "feat(daemon): consume daemon_commands at top of reconcile tick (2B.3)"
```

---

## Task 5: NOTIFY listener thread + wake-driven reconcile loop

**Files:**
- Modify: `src/localmail/daemon.py` (`_run_command_listener`, `stop`/`_handle_signal` set wake, `run_forever` wake loop + listener spawn)
- Test: `tests/test_daemon_command_listen.py` (NEW)

- [ ] **Step 1: Write the failing listener/wake tests**

Create `tests/test_daemon_command_listen.py`:

```python
"""LISTEN/NOTIFY wake: the listener sets the reconcile wake on NOTIFY, and
run_forever reconciles early instead of waiting out reload_seconds (2B.3)."""
from __future__ import annotations

import threading

import psycopg
import pytest

import localmail.daemon as daemon_mod
from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account
from localmail.api.admin.daemon import enqueue_command
from localmail.config import AccountConfig, LocalmailConfig
from localmail.daemon import Daemon


class _FakeBackend:
    name = "fake"; model = "fake"; dimension = 768
    def embed_documents(self, texts): return [[0.5] * 768 for _ in texts]
    def embed_query(self, _t): return [0.5] * 768
    def health_check(self) -> None: pass


def _cfg(db_dsn, *, listen=True):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    cfg.daemon.command_listen_enabled = listen
    cfg.daemon.command_listen_poll_seconds = 0.2  # snappy stop in tests
    cfg.daemon.reload_seconds = 30  # large: only a NOTIFY can cause an early tick
    return cfg


def _account(conn, name="a1"):
    cfg = AccountConfig(name=name, email=f"{name}@example.com",
                        imap_host="imap.example.com", imap_port=993,
                        auth_method="password")
    return create_account(conn, **account_create_kwargs(cfg)).id


@pytest.fixture
def quiet_threads(monkeypatch):
    monkeypatch.setattr(daemon_mod, "run_inbox_idle_loop", lambda ctx: ctx.stop.wait())
    monkeypatch.setattr(daemon_mod, "run_poll_loop", lambda ctx: ctx.stop.wait())


def test_notify_sets_reconcile_wake(db_conn, db_dsn, quiet_threads):
    """The listener thread sets _reconcile_wake when a NOTIFY arrives."""
    d = Daemon(cfg=_cfg(db_dsn), dsn=db_dsn,
               embedding_backend_factory=lambda c: _FakeBackend())
    listener = threading.Thread(target=d._run_command_listener, daemon=True)
    listener.start()
    try:
        # Give the listener a moment to LISTEN, then NOTIFY from another conn.
        import time
        time.sleep(0.5)
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()
        assert d._reconcile_wake.wait(timeout=5), "NOTIFY did not set the wake"
    finally:
        d.stop()
        listener.join(timeout=3)
        d.pool.close()


def test_run_forever_reconciles_early_on_notify(db_conn, db_dsn, quiet_threads):
    """With reload_seconds=30, only the NOTIFY path can make reconcile run fast."""
    _account(db_conn, "a1"); db_conn.commit()
    d = Daemon(cfg=_cfg(db_dsn), dsn=db_dsn,
               embedding_backend_factory=lambda c: _FakeBackend())
    reconciled = threading.Event()
    orig = d.reconcile

    def watched():
        orig()
        reconciled.set()

    d.reconcile = watched  # type: ignore[method-assign]
    t = threading.Thread(target=d.run_forever, daemon=True)
    t.start()
    try:
        import time
        time.sleep(0.5)  # let the loop reach its wait and the listener LISTEN
        reconciled.clear()
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()
        assert reconciled.wait(timeout=5), "run_forever did not reconcile on NOTIFY"
    finally:
        d.stop()
        t.join(timeout=5)
    assert not t.is_alive()


def test_listener_disabled_still_consumes_on_poll(db_conn, db_dsn, quiet_threads):
    """With the listener off, a command is still consumed on the next poll tick."""
    _account(db_conn, "a1"); db_conn.commit()
    cfg = _cfg(db_dsn, listen=False)
    cfg.daemon.reload_seconds = 0.1  # poll fast since there's no NOTIFY wake
    d = Daemon(cfg=cfg, dsn=db_dsn,
               embedding_backend_factory=lambda c: _FakeBackend())
    t = threading.Thread(target=d.run_forever, daemon=True)
    t.start()
    try:
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with db_conn.cursor() as cur:
                cur.execute("SELECT state FROM daemon_commands")
                row = cur.fetchone()
            db_conn.rollback()  # release snapshot so we see the daemon's commit
            if row and row[0] == "done":
                break
            time.sleep(0.1)
        assert row is not None and row[0] == "done"
    finally:
        d.stop()
        t.join(timeout=5)
```

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_command_listen.py`
Expected: FAIL — `_run_command_listener` doesn't exist; `run_forever` doesn't start a listener or wait on the wake.

- [ ] **Step 3: Implement the listener + wake loop**

In `src/localmail/daemon.py`:

(a) Make `stop()` and `_handle_signal` also poke the wake so the loop breaks promptly:

```python
    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info("received signal %s; stopping daemon", signum)
        self._stop_event.set()
        self._reconcile_wake.set()
```

```python
    def stop(self) -> None:
        """Signal every thread to stop (master event + all per-account events)."""
        self._stop_event.set()
        self._reconcile_wake.set()
        for bundle in list(self._account_threads.values()):
            bundle.stop_event.set()
```

(b) Add the listener method (place it near `reconcile`):

```python
    def _run_command_listener(self) -> None:
        """LISTEN the daemon_commands channel; set the reconcile wake on each
        NOTIFY so run_forever reconciles early instead of waiting out
        reload_seconds. A dedicated autocommit connection (LISTEN must be visible
        immediately and notifications are only delivered outside a transaction).
        statement_timeout is disabled on this long-lived connection. Reconnects
        with the same fresh-connect bounds on any error; exits on the stop event.
        The poll path remains authoritative — this loop only reduces latency."""
        poll = self.cfg.daemon.command_listen_poll_seconds
        while not self._stop_event.is_set():
            try:
                with self._connect() as conn:
                    conn.autocommit = True
                    conn.execute("SET statement_timeout = 0")
                    conn.execute("LISTEN daemon_commands")
                    while not self._stop_event.is_set():
                        for _note in conn.notifies(timeout=poll, stop_after=1):
                            self._reconcile_wake.set()
            except Exception:
                if self._stop_event.is_set():
                    break
                log.warning("command listener error; reconnecting",
                            exc_info=True)
                self._stop_event.wait(poll)  # brief backoff before retry
```

(c) Rewrite `run_forever` to spawn the listener and wait on the wake:

```python
    def run_forever(self) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)
        self.start_workers()  # initial account spawn + embed/extract workers
        listener: threading.Thread | None = None
        if self.cfg.daemon.command_listen_enabled:
            listener = threading.Thread(
                target=self._run_command_listener,
                name="command_listener", daemon=True,
            )
            listener.start()
            log.info("started command listener thread")
        log.info("daemon running; reconciling every %ds (wake on NOTIFY)",
                 self.cfg.daemon.reload_seconds)
        try:
            while True:
                # Wake on a NOTIFY (listener) or stop (signal/drain-stop), else
                # fall through after reload_seconds for the authoritative poll.
                self._reconcile_wake.wait(self.cfg.daemon.reload_seconds)
                self._reconcile_wake.clear()
                if self._stop_event.is_set():
                    break
                self.reconcile()
                if self._stop_event.is_set():
                    break  # drain-stop fired inside reconcile
        finally:
            log.info("waiting for worker threads to finish")
            for account_id in list(self._account_threads):
                self._teardown_account(account_id)
            for t in self._worker_threads:
                t.join(timeout=self.cfg.daemon.shutdown_grace_seconds)
            if listener is not None:
                listener.join(timeout=self.cfg.daemon.shutdown_grace_seconds)
            self.pool.close()
            log.info("daemon stopped")
```

- [ ] **Step 4: Run to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_command_listen.py`
Expected: PASS (3 tests).

- [ ] **Step 5: Regression — the existing run_forever test still passes**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_hot_reload.py::test_run_forever_reconciles_then_stops`
Expected: PASS. (That test sets `reload_seconds=0.05` and default `command_listen_enabled=True`; the wake loop times out every 0.05s → reconcile, and `d.stop()` sets the wake to break out. A listener connects to the test DB and idles harmlessly.)

- [ ] **Step 6: mypy + full suite**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean.
Run: `unset VIRTUAL_ENV && uv run pytest -q tests/`
Expected: PASS (prior baseline 1119 + the new tests; note the harmless psycopg pool `__del__` ResourceWarning at teardown is not a failure).

- [ ] **Step 7: Commit**

```bash
git add src/localmail/daemon.py tests/test_daemon_command_listen.py
git commit -m "feat(daemon): LISTEN/NOTIFY wake for command queue (2B.3)"
```

---

## Task 6: Docs — README + ROADMAP

**Files:**
- Modify: `README.md` (daemon `run` description — note the command queue / hot-reload latency)
- Modify: `ROADMAP.md` (mark 2B.3 shipped)

- [ ] **Step 1: Update README**

Find the `localmail run` daemon description in `README.md`. Add one clause noting that the running daemon now consumes a `daemon_commands` queue (`reload-now` / `restart-account` / `drain-stop`) via `LISTEN/NOTIFY` so account changes and operator actions converge without waiting out the full reconcile interval. Keep it to the README's existing register (end-user view). Do NOT document an HTTP/CLI surface — none ships in 2B.3 (that's 2B.4).

- [ ] **Step 2: Update ROADMAP**

In `ROADMAP.md`, mark **2B.3 (command queue)** as shipped/done with the date, mirroring how 2B.1 and 2B.2 are recorded. Next up remains 2B.4 (DaemonSupervisor + HTTP + CLI).

- [ ] **Step 3: Commit**

```bash
git add README.md ROADMAP.md
git commit -m "docs: record 2B.3 command queue (README + ROADMAP)"
```

---

## Self-review notes (verified against spec §2B.3)

- **Migration** matches the spec SQL exactly (table columns, both CHECKs, the partial `daemon_commands_queue_idx`). `requested_by` is nullable (daemon-side enqueues and tests pass `None`).
- **Three commands** all covered: `reload-now` (effect = trigger the same-tick reconcile), `restart-account` (teardown → same-tick respawn via the diff), `drain-stop` (set master stop). Spec semantics matched.
- **Consumption** uses `FOR UPDATE SKIP LOCKED` ordered by `requested_at` (Task 3 `claim_commands`), drained at the **top** of `reconcile` (Task 4), then marked `done`/`failed` with `result_msg` (poison/duplicate commands don't wedge the queue — each is marked terminal in its own try/except; a drain-level failure is swallowed and retried next tick).
- **LISTEN/NOTIFY** included per the user's decision: dedicated autocommit listener connection, `notifies(timeout=command_listen_poll_seconds, stop_after=1)`, sets `_reconcile_wake`; `run_forever` waits on the wake. Poll path remains authoritative (`test_listener_disabled_still_consumes_on_poll`).
- **Enqueue accessor only** — no HTTP/CLI (deferred to 2B.4), matching the slice scope.
- **No magic numbers:** the two new timing/behaviour knobs (`command_listen_enabled`, `command_listen_poll_seconds`) live on `DaemonConfig` with `extra="forbid"` round-trip tests.
- **Type consistency:** `DaemonCommand` field names match the `claim_commands` SELECT column list (`class_row`); `_apply_command` returns `str`; `mark_command(state=...)` takes the `CommandState` literal; `enqueue_command(command=...)` takes `CommandName`.
- **Single-funnel connects:** `_drain_commands` and `_run_command_listener` both open via `Daemon._connect()`, inheriting the #140/#142 connect/tcp/statement bounds (listener disables `statement_timeout` on its long-lived connection).

## Risks / watch-items

1. **Listener connection count.** With `command_listen_enabled=True` (default), every `run_forever` opens one extra long-lived connection (outside the pool). It's one fixed connection, not per-account; acceptable for the single-daemon model. Documented; disable via config for tight `max_connections` budgets.
2. **`restart-account` relies on the same-tick reconcile to respawn.** If the account-read in `reconcile` fails that tick (transient DB error after the drain committed), the account stays down until the next successful reconcile. This matches the existing "transient read failure is swallowed" contract and is logged. The command is still marked `done` (teardown succeeded) — restart is best-effort respawn, consistent with reconcile semantics. Note this in the 2B.4 plan if HTTP wants a stronger guarantee.
3. **NOTIFY-wake tests are timing-based.** They use generous timeouts (5s) against a 30s `reload_seconds` so a missed wake fails fast and unambiguously, but they are DB-dependent and skipped without `LOCALMAIL_TEST_DSN`. If they ever flake in CI, raise the wait timeout, not the reload interval.
4. **`drain-stop` during `start()` (non-blocking) has no effect** — there's no reconcile loop in `start()`, only in `run_forever`. Stopping a `start()`-driven daemon (supervisor/tests) goes through `stop()`. This is correct and matches the spec (process *start* is Plane B only).
```
