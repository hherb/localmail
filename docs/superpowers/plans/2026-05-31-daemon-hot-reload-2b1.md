# Daemon hot-reload (2B.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a running `localmail` daemon converge on the DB's syncable account set without a restart — spawn threads for newly-syncable accounts, tear down threads for accounts that became non-syncable, and respawn an account whose config (or credentials) changed.

**Architecture:** A pure diff planner (`daemon_reconcile.py`) computes spawn/teardown/respawn sets by comparing `(account_id, updated_at)` fingerprints. The `Daemon` gains a per-account thread registry with one `threading.Event` per account (the master event still drives the embed/extract workers and shutdown). `run_forever` becomes a reconcile loop that polls the DB every `reload_seconds` and resizes the shared pool when the account count changes. A small `touch_account_updated_at` helper makes credential changes (OAuth re-login, password rotation) participate in the `(id, updated_at)` diff.

**Tech Stack:** Python 3.12, `psycopg` v3 + `psycopg_pool`, `pydantic` v2, `pytest`. Slice 2B.1 of the daemon-control re-spec — see [docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](../specs/2026-05-30-daemon-control-2b-respec-design.md).

**Run tests with:** `unset VIRTUAL_ENV && uv run pytest …` (a stray `VIRTUAL_ENV` makes `uv` pick the wrong interpreter).

---

## File structure

- **Create** `src/localmail/daemon_reconcile.py` — pure `ReconcilePlan` dataclass + `plan_reconcile()`. No IO, no threads. Sibling to the existing pure `daemon_accounts.py`.
- **Modify** `src/localmail/config.py` — add `reload_seconds` + `shutdown_grace_seconds` to `DaemonConfig`.
- **Modify** `src/localmail/daemon.py` — `AccountThreads` dataclass; per-account registry; `_spawn_account` / `_teardown_account` / `_pool_sizes` / `_resize_pool` / `reconcile`; reconcile loop in `run_forever`; updated `stop` / `join`.
- **Modify** `src/localmail/api/admin/accounts.py` — add `touch_account_updated_at(conn, account_id)`.
- **Modify** `src/localmail/api/admin/oauth.py` — `complete_oauth` bumps `updated_at`.
- **Modify** `src/localmail/cli.py` + `src/localmail/serve/admin/accounts_router.py` — bump `updated_at` after a password store.
- **Modify** `config.example.toml` — document the two new `[daemon]` knobs.
- **Create** `tests/test_daemon_reconcile.py` — pure planner tests.
- **Create** `tests/test_daemon_hot_reload.py` — daemon reconcile orchestration tests.
- **Modify** `tests/test_config*` — knob defaults (locate exact file in Task 2).

---

## Task 1: Pure reconcile planner

**Files:**
- Create: `src/localmail/daemon_reconcile.py`
- Test: `tests/test_daemon_reconcile.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_daemon_reconcile.py
"""Unit tests for the pure account-reconcile diff planner (2B.1)."""

from __future__ import annotations

from datetime import datetime, timezone

from localmail.daemon_reconcile import ReconcilePlan, plan_reconcile


def _ts(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def test_empty_both_sides_is_noop():
    plan = plan_reconcile({}, {})
    assert plan == ReconcilePlan(to_spawn=(), to_teardown=(), to_respawn=())
    assert plan.is_empty


def test_spawn_only_for_new_account():
    plan = plan_reconcile({}, {7: _ts(1)})
    assert plan.to_spawn == (7,)
    assert plan.to_teardown == ()
    assert plan.to_respawn == ()
    assert not plan.is_empty


def test_teardown_only_for_vanished_account():
    plan = plan_reconcile({7: _ts(1)}, {})
    assert plan.to_teardown == (7,)
    assert plan.to_spawn == ()
    assert plan.to_respawn == ()


def test_respawn_when_updated_at_changes():
    plan = plan_reconcile({7: _ts(1)}, {7: _ts(2)})
    assert plan.to_respawn == (7,)
    assert plan.to_spawn == ()
    assert plan.to_teardown == ()


def test_noop_when_identical():
    plan = plan_reconcile({7: _ts(1), 9: _ts(3)}, {7: _ts(1), 9: _ts(3)})
    assert plan.is_empty


def test_combined_plan_is_sorted_and_disjoint():
    running = {1: _ts(1), 2: _ts(1), 3: _ts(1)}
    desired = {2: _ts(2), 3: _ts(1), 4: _ts(1)}  # 1 gone, 2 changed, 3 same, 4 new
    plan = plan_reconcile(running, desired)
    assert plan.to_spawn == (4,)
    assert plan.to_teardown == (1,)
    assert plan.to_respawn == (2,)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_reconcile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.daemon_reconcile'`.

- [ ] **Step 3: Write the implementation**

```python
# src/localmail/daemon_reconcile.py
"""Pure account-reconcile diff for the daemon's hot-reload (2B.1).

No IO, no threads. The daemon reads the desired syncable account set from the
DB and compares it against the threads it currently runs; this module turns the
two ``{account_id: updated_at}`` fingerprint maps into a spawn/teardown/respawn
plan. Keyed on ``updated_at`` so any change to an account row (config edit or a
credential touch) forces a respawn; only inequality matters, so writer clock
skew is harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class ReconcilePlan:
    to_spawn: tuple[int, ...]      # in desired, not running
    to_teardown: tuple[int, ...]   # running, not in desired
    to_respawn: tuple[int, ...]    # in both, updated_at differs

    @property
    def is_empty(self) -> bool:
        return not (self.to_spawn or self.to_teardown or self.to_respawn)


def plan_reconcile(
    running: Mapping[int, datetime],
    desired: Mapping[int, datetime],
) -> ReconcilePlan:
    """Diff the running fingerprints against the desired ones.

    ``running`` / ``desired`` map ``account_id`` to the ``updated_at`` the
    bundle was spawned with / the current DB value. Returns sorted, disjoint
    id tuples so the caller's apply order is deterministic.
    """
    running_ids = set(running)
    desired_ids = set(desired)
    to_spawn = tuple(sorted(desired_ids - running_ids))
    to_teardown = tuple(sorted(running_ids - desired_ids))
    to_respawn = tuple(
        sorted(
            aid
            for aid in running_ids & desired_ids
            if running[aid] != desired[aid]
        )
    )
    return ReconcilePlan(
        to_spawn=to_spawn, to_teardown=to_teardown, to_respawn=to_respawn
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_reconcile.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/daemon_reconcile.py tests/test_daemon_reconcile.py
git commit -m "feat(daemon): pure account-reconcile diff planner (2B.1)"
```

---

## Task 2: Config knobs `reload_seconds` + `shutdown_grace_seconds`

**Files:**
- Modify: `src/localmail/config.py` (`DaemonConfig`)
- Test: locate with `unset VIRTUAL_ENV && uv run pytest --collect-only -q | grep -i daemon.*config` or grep `grep -rln "DaemonConfig\|daemon\b" tests/test_config*.py`; if none exists, create `tests/test_config_daemon.py`.

- [ ] **Step 1: Write the failing test**

If a daemon-config test file exists, add these; otherwise create `tests/test_config_daemon.py`:

```python
# tests/test_config_daemon.py
"""DaemonConfig knob defaults (2B.1)."""

from __future__ import annotations

from localmail.config import DaemonConfig


def test_reload_seconds_default():
    assert DaemonConfig().reload_seconds == 30


def test_shutdown_grace_seconds_default():
    assert DaemonConfig().shutdown_grace_seconds == 30.0


def test_knobs_are_overridable():
    cfg = DaemonConfig(reload_seconds=5, shutdown_grace_seconds=2.5)
    assert cfg.reload_seconds == 5
    assert cfg.shutdown_grace_seconds == 2.5
```

- [ ] **Step 2: Run to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config_daemon.py -v`
Expected: FAIL — `AttributeError: 'DaemonConfig' object has no attribute 'reload_seconds'`.

- [ ] **Step 3: Add the fields**

In `src/localmail/config.py`, append to the `DaemonConfig` class body (after `startup_backoff_max_s`):

```python
    # How often the running daemon re-reads the account set and reconciles
    # its per-account threads (seconds). Hot-reload latency upper bound.
    reload_seconds: int = 30
    # Per-thread join timeout on teardown / shutdown (seconds). Reused by the
    # 2B.4 supervisor's stop() (SIGTERM -> wait -> SIGKILL).
    shutdown_grace_seconds: float = 30.0
```

- [ ] **Step 4: Run to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config_daemon.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_config_daemon.py
git commit -m "feat(config): daemon reload_seconds + shutdown_grace_seconds (2B.1)"
```

---

## Task 3: Per-account thread registry (refactor `Daemon`, no behaviour change)

This task splits the single shared `stop_event` into one event per account and
replaces the flat `self.threads` list with a `{account_id: AccountThreads}`
registry plus a separate worker-thread list. **No reconcile yet** — the daemon
still spawns the initial set once. Existing daemon tests must stay green.

**Files:**
- Modify: `src/localmail/daemon.py`

- [ ] **Step 1: Add the `AccountThreads` dataclass and imports**

In `src/localmail/daemon.py`, after the existing imports add:

```python
from dataclasses import dataclass
from datetime import datetime

from .daemon_reconcile import plan_reconcile
```

And above `class Daemon:` add:

```python
@dataclass
class AccountThreads:
    account_id: int
    updated_at: datetime
    stop_event: threading.Event
    idle_thread: threading.Thread
    poll_thread: threading.Thread
```

- [ ] **Step 2: Replace registry fields in `__init__`**

In `Daemon.__init__`, replace:

```python
        self.threads: list[threading.Thread] = []
```

with:

```python
        self._account_threads: dict[int, AccountThreads] = {}
        self._worker_threads: list[threading.Thread] = []
        self._current_max_size = resolved_max_size
```

(`resolved_max_size` is the local already computed just above for `open_pool`.)

- [ ] **Step 3: Add the spawn/teardown/pool-sizing helpers**

Add these methods to `Daemon` (place them after `_handle_signal`):

```python
    def _gmail_secrets(self):
        return (
            self.cfg.gmail_oauth.client_secrets_file
            if self.cfg.gmail_oauth
            else None
        )

    def _spawn_account(self, account_row: Account) -> None:
        stop_event = threading.Event()
        ctx = WorkerContext(
            account=account_config_from_row(account_row),
            account_id=account_row.id,
            pool=self.pool,
            attachments_root=self.cfg.attachments.root,
            idle_renew_seconds=self.cfg.daemon.idle_renew_seconds,
            poll_seconds=self.cfg.daemon.poll_seconds,
            gmail_client_secrets=self._gmail_secrets(),
            stop=stop_event,
            ssl=self.ssl,
        )
        t_idle = threading.Thread(
            target=run_inbox_idle_loop, args=(ctx,),
            name=f"idle-{account_row.name}", daemon=True,
        )
        t_poll = threading.Thread(
            target=run_poll_loop, args=(ctx,),
            name=f"poll-{account_row.name}", daemon=True,
        )
        t_idle.start()
        t_poll.start()
        self._account_threads[account_row.id] = AccountThreads(
            account_id=account_row.id,
            updated_at=account_row.updated_at,
            stop_event=stop_event,
            idle_thread=t_idle,
            poll_thread=t_poll,
        )
        log.info("started workers for %s", account_row.name)

    def _teardown_account(self, account_id: int) -> None:
        bundle = self._account_threads.pop(account_id, None)
        if bundle is None:
            return
        bundle.stop_event.set()
        grace = self.cfg.daemon.shutdown_grace_seconds
        bundle.idle_thread.join(timeout=grace)
        bundle.poll_thread.join(timeout=grace)
        log.info("stopped workers for account_id=%s", account_id)

    def _running_fingerprints(self) -> dict[int, datetime]:
        return {
            aid: bundle.updated_at
            for aid, bundle in self._account_threads.items()
        }

    def _pool_sizes(self, n_accounts: int) -> tuple[int, int]:
        configured = self.cfg.daemon.pool_max_size
        if configured is None:
            max_size = compute_daemon_pool_size(
                n_accounts=n_accounts,
                run_embed=self.cfg.search.run_embed_worker,
                run_extract=self.cfg.search.run_extract_worker,
            )
        else:
            max_size = configured
        min_size = min(
            n_accounts * 2
            + (1 if self.cfg.search.run_embed_worker else 0)
            + (1 if self.cfg.search.run_extract_worker else 0)
            or 1,
            max_size,
        )
        return min_size, max_size

    def _resize_pool(self) -> None:
        if self.cfg.daemon.pool_max_size is not None:
            return  # operator pinned the size; never auto-resize
        min_size, max_size = self._pool_sizes(len(self._account_threads))
        if max_size != self._current_max_size:
            self.pool.resize(min_size=min_size, max_size=max_size)
            self._current_max_size = max_size
            log.info("daemon pool resized: max_size=%d (accounts=%d)",
                     max_size, len(self._account_threads))
```

- [ ] **Step 4: Rewrite `start_workers` to use the registry**

Replace the body of `start_workers` so the per-account loop calls
`_spawn_account` and the embed/extract spawns append to `self._worker_threads`.
Extract the embed/extract spawn block into a new `_spawn_worker_threads`
method, called at the end of `start_workers`:

```python
    def start_workers(self) -> None:
        if self._started:
            return
        self._started = True
        for account_row in self._syncable:
            self._spawn_account(account_row)
        self._spawn_worker_threads()

    def _spawn_worker_threads(self) -> None:
        if self.cfg.search.run_embed_worker:
            from localmail.search.embed_worker import run_embed_worker  # noqa: PLC0415
            from localmail.search.lang_detect import make_detector  # noqa: PLC0415

            if self._embedding_backend_factory is None:
                from localmail.search.embeddings import FastEmbedBackend  # noqa: PLC0415

                backend = FastEmbedBackend(self.cfg.search)
            else:
                backend = self._embedding_backend_factory(self.cfg.search)
            lang_detector = make_detector(self.cfg.search)
            t_embed = threading.Thread(
                target=run_embed_worker,
                args=(self._stop_event, self.pool, self.cfg.search, backend),
                kwargs={"lang_detector": lang_detector},
                name="embed_worker",
                daemon=True,
            )
            t_embed.start()
            self._worker_threads.append(t_embed)
            log.info(
                "started embed_worker thread (lang_detector=%s)",
                "on" if lang_detector is not None else "off",
            )

        if self.cfg.search.run_extract_worker:
            from localmail.search.extract_worker import run_extract_worker  # noqa: PLC0415

            t_extract = threading.Thread(
                target=run_extract_worker,
                kwargs={
                    "pool": self.pool,
                    "cfg": self.cfg.search,
                    "stop_event": self._stop_event,
                },
                name="extract_worker",
                daemon=True,
            )
            t_extract.start()
            self._worker_threads.append(t_extract)
            log.info("started extract_worker thread")
```

- [ ] **Step 5: Update `stop` and `join` to the split model**

Replace `stop` and `join`:

```python
    def stop(self) -> None:
        """Signal every thread to stop (master event + all per-account events)."""
        self._stop_event.set()
        for bundle in self._account_threads.values():
            bundle.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for all worker threads to finish."""
        for bundle in list(self._account_threads.values()):
            bundle.idle_thread.join(timeout=timeout)
            bundle.poll_thread.join(timeout=timeout)
        for t in self._worker_threads:
            t.join(timeout=timeout)
```

- [ ] **Step 6: Keep `run_forever` working with the new fields (interim)**

In `run_forever`, replace the `finally` block's thread loop (which referenced
`self.threads`) so it tears down accounts and joins workers:

```python
        finally:
            log.info("waiting for worker threads to finish")
            for account_id in list(self._account_threads):
                self._teardown_account(account_id)
            for t in self._worker_threads:
                t.join(timeout=self.cfg.daemon.shutdown_grace_seconds)
            self.pool.close()
            log.info("daemon stopped")
```

Leave the `while not self._stop_event.is_set(): self._stop_event.wait(60)`
loop as-is for now (Task 4 replaces it). Leave the early `if not self._syncable:`
return as-is for now (Task 4 removes it).

- [ ] **Step 7: Run the existing daemon tests to verify no regression**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon.py tests/test_daemon_pool.py tests/test_daemon_embed_thread.py tests/test_daemon_extract_thread.py tests/test_daemon_startup_backoff.py -v`
Expected: PASS (all existing daemon tests green — the refactor preserves behaviour). If any test referenced `daemon.threads`, update it to use `daemon._worker_threads` / `daemon._account_threads` and note it in the commit.

- [ ] **Step 8: Type-check**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail/daemon.py`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add src/localmail/daemon.py tests/
git commit -m "refactor(daemon): per-account thread registry + per-account stop events (2B.1)"
```

---

## Task 4: Reconcile method + reconcile loop

**Files:**
- Modify: `src/localmail/daemon.py`
- Test: `tests/test_daemon_hot_reload.py`

- [ ] **Step 1: Write the failing orchestration tests**

```python
# tests/test_daemon_hot_reload.py
"""Daemon account hot-reload: reconcile spawns/tears-down/respawns (2B.1)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

import localmail.daemon as daemon_mod
from localmail.config import LocalmailConfig
from localmail.daemon import Daemon


class _FakeBackend:
    name = "fake"
    model = "fake"
    dimension = 768

    def embed_documents(self, texts):
        return [[0.5] * 768 for _ in texts]

    def embed_query(self, _text):
        return [0.5] * 768

    def health_check(self) -> None:
        pass


def _cfg(db_dsn):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    return cfg


def _row(account_id: int, day: int, name: str | None = None):
    """A minimal stand-in carrying the fields _spawn_account reads."""
    from localmail.api.admin.accounts import Account

    return Account(
        id=account_id,
        name=name or f"acct{account_id}",
        email_address=f"a{account_id}@example.com",
        auth_method="password",
        oauth_provider=None,
        imap_host="imap.example.com",
        imap_port=993,
        folder_allow=None,
        folder_deny=None,
        folder_deny_flags=None,
        sync_enabled=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, day, tzinfo=timezone.utc),
    )


@pytest.fixture
def quiet_threads(monkeypatch):
    """Replace the IDLE/poll loops with functions that block on ctx.stop so no
    real IMAP/DB IO happens; threads exit promptly when their event is set."""

    def fake_idle(ctx):
        ctx.stop.wait()

    def fake_poll(ctx):
        ctx.stop.wait()

    monkeypatch.setattr(daemon_mod, "run_inbox_idle_loop", fake_idle)
    monkeypatch.setattr(daemon_mod, "run_poll_loop", fake_poll)


def _make_daemon(db_dsn, monkeypatch, desired):
    """Construct a Daemon whose list_syncable_accounts returns `desired()`."""
    monkeypatch.setattr(daemon_mod, "list_syncable_accounts", lambda conn: desired())
    d = Daemon(
        cfg=_cfg(db_dsn),
        dsn=db_dsn,
        embedding_backend_factory=lambda c: _FakeBackend(),
    )
    return d


def test_reconcile_spawns_new_account(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": []}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    try:
        d.start_workers()
        assert d._account_threads == {}
        state["rows"] = [_row(1, 1)]
        d.reconcile()
        assert set(d._account_threads) == {1}
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


def test_reconcile_tears_down_vanished_account(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": [_row(1, 1), _row(2, 1)]}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    try:
        d.start_workers()
        assert set(d._account_threads) == {1, 2}
        bundle2 = d._account_threads[2]
        state["rows"] = [_row(1, 1)]
        d.reconcile()
        assert set(d._account_threads) == {1}
        assert bundle2.stop_event.is_set()  # the removed account was told to stop
        assert not bundle2.idle_thread.is_alive()
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


def test_reconcile_respawns_on_updated_at_change(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": [_row(1, 1)]}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    try:
        d.start_workers()
        old = d._account_threads[1]
        state["rows"] = [_row(1, 2)]  # same id, newer updated_at
        d.reconcile()
        assert set(d._account_threads) == {1}
        assert d._account_threads[1] is not old
        assert old.stop_event.is_set()
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


def test_reconcile_survives_db_read_error(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": [_row(1, 1)]}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    try:
        d.start_workers()
        assert set(d._account_threads) == {1}

        def boom(conn):
            raise RuntimeError("db down")

        monkeypatch.setattr(daemon_mod, "list_syncable_accounts", boom)
        d.reconcile()  # must not raise
        assert set(d._account_threads) == {1}  # existing thread kept
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


def test_reconcile_resizes_pool_when_count_changes(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": []}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    calls = []
    monkeypatch.setattr(d.pool, "resize",
                        lambda **kw: calls.append(kw))
    try:
        d.start_workers()
        state["rows"] = [_row(i, 1) for i in range(1, 6)]  # 5 accounts
        d.reconcile()
        assert calls, "expected pool.resize to be called when count grew"
        # no-op reconcile must not resize again
        calls.clear()
        d.reconcile()
        assert calls == []
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_hot_reload.py -v`
Expected: FAIL — `AttributeError: 'Daemon' object has no attribute 'reconcile'`.

- [ ] **Step 3: Add the `reconcile` method**

Add to `Daemon` (after `_resize_pool`):

```python
    def reconcile(self) -> None:
        """Converge the running per-account threads on the DB's syncable set.

        A transient DB read failure is logged and swallowed for this tick;
        existing threads keep running and the next tick retries. Apply order is
        teardown -> respawn -> spawn so freed pool slots are reused first.
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                desired_rows = list_syncable_accounts(conn)
        except Exception:
            log.warning(
                "reconcile: failed to read accounts; keeping current threads",
                exc_info=True,
            )
            return

        rows_by_id = {row.id: row for row in desired_rows}
        desired = {row.id: row.updated_at for row in desired_rows}
        plan = plan_reconcile(self._running_fingerprints(), desired)
        if plan.is_empty:
            return

        for account_id in plan.to_teardown:
            self._teardown_account(account_id)
        for account_id in plan.to_respawn:
            self._teardown_account(account_id)
            self._spawn_account(rows_by_id[account_id])
        for account_id in plan.to_spawn:
            self._spawn_account(rows_by_id[account_id])

        self._resize_pool()
        log.info(
            "reconcile: spawned=%d torn_down=%d respawned=%d",
            len(plan.to_spawn), len(plan.to_teardown), len(plan.to_respawn),
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_hot_reload.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Convert `run_forever` to the reconcile loop**

Replace `run_forever` in full:

```python
    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self.start_workers()  # initial account spawn + embed/extract workers
        log.info("daemon running; reconciling every %ds",
                 self.cfg.daemon.reload_seconds)
        try:
            while not self._stop_event.wait(self.cfg.daemon.reload_seconds):
                self.reconcile()
        finally:
            log.info("waiting for worker threads to finish")
            for account_id in list(self._account_threads):
                self._teardown_account(account_id)
            for t in self._worker_threads:
                t.join(timeout=self.cfg.daemon.shutdown_grace_seconds)
            self.pool.close()
            log.info("daemon stopped")
```

Note: the old `if not self._syncable: ... return` early exit is intentionally
removed — a daemon with zero accounts now stays up and picks accounts up live.

- [ ] **Step 6: Add a reconcile-loop test**

Append to `tests/test_daemon_hot_reload.py`:

```python
def test_run_forever_reconciles_then_stops(db_dsn, monkeypatch, quiet_threads):
    """run_forever picks up an account added after start, then stops cleanly."""
    state = {"rows": []}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    # Tight loop so the test is fast.
    d.cfg.daemon.reload_seconds = 0.05
    seen = threading.Event()
    orig_reconcile = d.reconcile

    def watched_reconcile():
        orig_reconcile()
        if d._account_threads:
            seen.set()

    monkeypatch.setattr(d, "reconcile", watched_reconcile)

    t = threading.Thread(target=d.run_forever, daemon=True)
    t.start()
    try:
        state["rows"] = [_row(1, 1)]
        assert seen.wait(timeout=3), "account was not picked up by run_forever"
        assert set(d._account_threads) == {1}
    finally:
        d.stop()
        t.join(timeout=3)
    assert not t.is_alive()
    assert d._account_threads == {}  # torn down on shutdown
```

Note: `reload_seconds` is typed `int` but `Event.wait` accepts a float; the
test assigns `0.05` directly to the instance attribute to keep the loop tight.

- [ ] **Step 7: Run the new test**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_hot_reload.py::test_run_forever_reconciles_then_stops -v`
Expected: PASS.

- [ ] **Step 8: Full daemon suite + mypy**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon*.py -v && uv run mypy src/localmail/daemon.py`
Expected: PASS, mypy clean.

- [ ] **Step 9: Commit**

```bash
git add src/localmail/daemon.py tests/test_daemon_hot_reload.py
git commit -m "feat(daemon): reconcile loop for live account hot-reload (2B.1)"
```

---

## Task 5: Credential-refresh bumps `updated_at`

So an OAuth re-login / password rotation participates in the `(id, updated_at)`
diff and triggers a respawn with the new secret.

**Files:**
- Modify: `src/localmail/api/admin/accounts.py` (add `touch_account_updated_at`)
- Modify: `src/localmail/api/admin/oauth.py` (`complete_oauth`)
- Modify: `src/localmail/cli.py` + `src/localmail/serve/admin/accounts_router.py` (password store sites)
- Test: `tests/test_admin_accounts.py`, `tests/test_admin_oauth.py` (or nearest existing)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_admin_accounts.py` (uses the existing `db_conn` fixture and
`create_account`; mirror the file's existing account-setup helper):

```python
def test_touch_account_updated_at_bumps_timestamp(db_conn):
    from localmail.api.admin.accounts import (
        create_account, get_account, touch_account_updated_at,
    )
    acct = create_account(
        db_conn, name="touch-me", email_address="t@example.com",
        auth_method="password", imap_host="imap.example.com", imap_port=993,
    )
    before = get_account(db_conn, acct.id).updated_at
    touch_account_updated_at(db_conn, acct.id)
    after = get_account(db_conn, acct.id).updated_at
    assert after > before
```

(If `create_account`'s required kwargs differ, copy the exact call used
elsewhere in `test_admin_accounts.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py::test_touch_account_updated_at_bumps_timestamp -v`
Expected: FAIL — `ImportError: cannot import name 'touch_account_updated_at'`.

- [ ] **Step 3: Add the helper**

In `src/localmail/api/admin/accounts.py`, add (near `update_account`):

```python
def touch_account_updated_at(conn: psycopg.Connection, account_id: int) -> None:
    """Bump accounts.updated_at so the daemon's hot-reload notices a change
    that did not otherwise edit the row (e.g. a credential rotation)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET updated_at = now() WHERE id = %s", (account_id,)
        )
        if cur.rowcount == 0:
            raise NotFound(f"account {account_id} not found")
```

- [ ] **Step 4: Run to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py::test_touch_account_updated_at_bumps_timestamp -v`
Expected: PASS.

- [ ] **Step 5: Wire `complete_oauth` to bump on re-login**

In `src/localmail/api/admin/oauth.py`, in `complete_oauth`, after
`_secrets.set_refresh_token(account.name, refresh_token)` and before
`return account`, add:

```python
    from localmail.api.admin.accounts import touch_account_updated_at  # noqa: PLC0415
    touch_account_updated_at(conn, account.id)
```

(Top-level import is fine too if it does not create a cycle; verify with
`unset VIRTUAL_ENV && uv run python -c "import localmail.api.admin.oauth"`.)

- [ ] **Step 6: Wire the credential-store call sites**

Find every place a password or refresh token is persisted (the admin service
uses `store_password`; the CLI `add-account` / `oauth-login` may call the
keyring wrapper `_secrets.set_password` / `_secrets.set_refresh_token`
directly):

```bash
cd /Users/hherb/src/localmail
grep -rn "store_password(\|set_password(\|set_refresh_token(" src/localmail/cli.py src/localmail/serve/admin/accounts_router.py
```

Confirmed source site: `src/localmail/serve/admin/accounts_router.py:212`
(`svc.store_password(account, body.password)`). The CLI's `add-account` /
`oauth-login` sites are whatever the grep surfaces.

At each site **where a `conn` (or pool connection) is in scope and a DB
`account.id` is known**, immediately after the credential is stored add:

```python
        from localmail.api.admin.accounts import touch_account_updated_at  # if not already imported
        touch_account_updated_at(conn, account.id)
```

Then make sure the surrounding code commits: if the function already commits
after this point, do nothing extra; otherwise add `conn.commit()` after the
touch. Do **not** add a second commit if one already runs.

If a CLI site stores a secret for an account it just created (so `updated_at`
is already fresh from the `INSERT`), the touch is harmless but unnecessary —
add it only on the *rotation / re-login* paths (`add-account` on an existing
account, `oauth-login`). Use judgement per site; the goal is that re-storing a
credential for an already-running account bumps `updated_at`.

- [ ] **Step 7: Add a `complete_oauth` bump test**

In `tests/test_admin_oauth.py` (use the file's existing `FakeGoogleOAuth` /
flow double + `db_conn`), add a test that runs `complete_oauth` for an existing
oauth2 account and asserts `get_account(...).updated_at` increased. Model it on
the existing happy-path `complete_oauth` test in that file (copy its setup
verbatim, then add the before/after `updated_at` assertion).

- [ ] **Step 8: Run the affected suites**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py tests/test_admin_oauth.py tests/test_cli_accounts.py tests/test_serve_admin_accounts.py -v`
Expected: PASS (locate exact serve/cli test filenames if they differ; run whatever covers the two edited call sites).

- [ ] **Step 9: Commit**

```bash
git add src/localmail/api/admin/accounts.py src/localmail/api/admin/oauth.py src/localmail/cli.py src/localmail/serve/admin/accounts_router.py tests/
git commit -m "feat(accounts): bump updated_at on credential change for hot-reload (2B.1)"
```

---

## Task 6: Document the new knobs + full-suite gate

**Files:**
- Modify: `config.example.toml`

- [ ] **Step 1: Document the knobs**

In `config.example.toml`, under the `[daemon]` section, add:

```toml
reload_seconds         = 30   # re-read accounts + reconcile threads this often
shutdown_grace_seconds = 30   # per-thread join timeout on teardown/shutdown
```

- [ ] **Step 2: Confirm the example config still loads**

Run: `unset VIRTUAL_ENV && uv run python -c "from localmail.config import load_config; load_config('config.example.toml')"`
Expected: no error (adjust to the project's actual loader signature if needed; otherwise `tomllib.load` + `LocalmailConfig.model_validate`).

- [ ] **Step 3: Full suite + mypy gate**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/ && uv run mypy src/localmail`
Expected: all green (baseline was 1063 passed; expect 1063 + the new tests). mypy clean.

- [ ] **Step 4: Commit**

```bash
git add config.example.toml
git commit -m "docs(config): document daemon reload_seconds + shutdown_grace_seconds (2B.1)"
```

---

## Self-review notes (already folded in)

- **Spec coverage:** Task 1 = pure planner; Tasks 3–4 = per-account events, registry, reconcile loop, pool resize, DB-failure isolation; Task 2 = `reload_seconds` + `shutdown_grace_seconds`; Task 5 = credential-refresh `updated_at` participation; Task 6 = config docs. The spec's "crashed loop not respawned" and "teardown latency bounded by IDLE tick" are documented non-goals for 2B.1 — no task needed.
- **Signature consistency:** `plan_reconcile(running, desired)` and `ReconcilePlan(to_spawn, to_teardown, to_respawn, is_empty)` are used identically in Tasks 1, 3, 4. `_spawn_account(account_row: Account)`, `_teardown_account(account_id: int)`, `touch_account_updated_at(conn, account_id)` are consistent across tasks.
- **Deviation from spec:** the spec said "give `store_password` a `conn`"; this plan instead adds a dedicated `touch_account_updated_at` helper and calls it at the credential-store sites, keeping `store_password` keyring-only (single responsibility, no churn across its 5 call sites). Same behaviour, cleaner boundary.
- **CLAUDE.md / README** sync-model updates are handled in the session wrap-up, not as plan tasks (they are cross-cutting prose, not part of the TDD loop).
