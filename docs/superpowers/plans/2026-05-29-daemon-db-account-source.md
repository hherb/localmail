# Daemon Reads Accounts From the DB (Sub-plan 2A.2b) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch the `localmail run` daemon from reading accounts out of `config.toml` (`cfg.accounts`) to enumerating the `accounts` table (live + `sync_enabled`), and retire the config-driven column overwrite in `sync.py:upsert_account`.

**Architecture:** Keep the `AccountConfig` boundary that `imap_client`, `idle`, `poller`, and `sync` already depend on. Bridge DB rows to it with one pure adapter (`account_config_from_row`), enumerate syncable rows via a new query (`list_syncable_accounts`), carry the DB `account_id` on `WorkerContext`, and have workers read `ctx.account_id` instead of calling `upsert_account`. Daemon-only scope; the one-shot `localmail sync` CLI stays TOML-driven (2A.2d).

**Tech Stack:** Python 3.12, `psycopg` v3, `pydantic` v2, `pytest`, real Postgres at `LOCALMAIL_TEST_DSN`.

Spec: [docs/superpowers/specs/2026-05-29-daemon-db-account-source-design.md](../specs/2026-05-29-daemon-db-account-source-design.md)

---

## File structure

- **Create** `src/localmail/daemon_accounts.py` — pure `account_config_from_row(Account) -> AccountConfig`.
- **Create** `tests/test_daemon_accounts.py` — adapter unit tests.
- **Modify** `src/localmail/api/admin/accounts.py` — add `list_syncable_accounts(conn)`.
- **Modify** `tests/test_admin_accounts.py` — test `list_syncable_accounts`.
- **Modify** `src/localmail/sync.py` — neuter `upsert_account` overwrite.
- **Modify** `tests/test_sync.py` — test the neutered `upsert_account`.
- **Modify** `src/localmail/worker.py` — add `account_id: int` to `WorkerContext`.
- **Modify** `src/localmail/daemon.py` — enumerate DB accounts; size pool + build contexts from them.
- **Modify** `tests/test_daemon.py` — update `make_ctx`, inline ctx; assert workers stop calling `upsert_account`.
- **Modify** `tests/test_daemon_pool.py` — daemon enumeration/sizing tests; guard the empty-table assumption.
- **Modify** `src/localmail/idle.py` / `src/localmail/poller.py` — use `ctx.account_id`; drop `upsert_account`.
- **Modify** `CLAUDE.md` — flip the "DB canonical" invariant + retire the risk-#2 note.

---

## Task 1: Pure adapter `account_config_from_row`

**Files:**
- Create: `src/localmail/daemon_accounts.py`
- Test: `tests/test_daemon_accounts.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_daemon_accounts.py`:

```python
"""Unit tests for the pure DB-row -> AccountConfig adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from localmail.api.admin.accounts import Account
from localmail.config import AccountConfig
from localmail.daemon_accounts import account_config_from_row


def _row(**over) -> Account:
    base = dict(
        id=1,
        name="acct",
        email_address="me@example.com",
        auth_method="password",
        oauth_provider=None,
        imap_host="imap.example.com",
        imap_port=993,
        folder_allow=None,
        folder_deny=None,
        folder_deny_flags=None,
        sync_enabled=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    base.update(over)
    return Account(**base)


def test_maps_password_row():
    assert account_config_from_row(_row()) == AccountConfig(
        name="acct",
        email="me@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        auth_method="password",
    )


def test_none_folder_lists_become_empty():
    cfg = account_config_from_row(
        _row(folder_allow=None, folder_deny=None, folder_deny_flags=None)
    )
    assert cfg.folder_allow == []
    assert cfg.folder_deny == []
    assert cfg.folder_deny_flags == []


def test_populated_folder_lists_pass_through():
    cfg = account_config_from_row(
        _row(folder_allow=["INBOX"], folder_deny=["Spam"], folder_deny_flags=["\\Trash"])
    )
    assert cfg.folder_allow == ["INBOX"]
    assert cfg.folder_deny == ["Spam"]
    assert cfg.folder_deny_flags == ["\\Trash"]


def test_oauth2_row_maps_provider():
    cfg = account_config_from_row(_row(auth_method="oauth2", oauth_provider="gmail"))
    assert cfg.auth_method == "oauth2"
    assert cfg.oauth_provider == "gmail"


def test_poll_seconds_is_none():
    assert account_config_from_row(_row()).poll_seconds is None


def test_archive_row_raises():
    with pytest.raises(ValueError, match="archive"):
        account_config_from_row(
            _row(auth_method="archive", imap_host=None, imap_port=None)
        )


def test_live_row_missing_host_raises():
    with pytest.raises(ValueError, match="imap_host"):
        account_config_from_row(_row(imap_host=None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_accounts.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.daemon_accounts'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/localmail/daemon_accounts.py`:

```python
"""Bridge DB account rows to the daemon's AccountConfig worker boundary.

Pure: no IO. The daemon enumerates syncable accounts from the DB
(`api.admin.accounts.list_syncable_accounts`) and maps each row through
`account_config_from_row`, so the existing AccountConfig-based worker code
(imap_client, idle, poller, sync) is unchanged.
"""

from __future__ import annotations

from typing import Literal, cast

from localmail.api.admin.accounts import Account
from localmail.config import AccountConfig


def account_config_from_row(account: Account) -> AccountConfig:
    """Map a DB ``Account`` row to the daemon's ``AccountConfig``.

    Raises ``ValueError`` for archive accounts (no IMAP host) — callers
    filter these out via ``list_syncable_accounts`` before mapping; the
    guard is defensive. Per-account ``poll_seconds`` has no DB column, so it
    is always ``None`` (the daemon falls back to the daemon-wide default).
    """
    if account.auth_method == "archive":
        raise ValueError(
            f"account {account.name!r} is an archive account and has no IMAP source"
        )
    if account.imap_host is None or account.imap_port is None:
        raise ValueError(
            f"live account {account.name!r} is missing imap_host/imap_port"
        )
    return AccountConfig(
        name=account.name,
        email=account.email_address,
        imap_host=account.imap_host,
        imap_port=account.imap_port,
        auth_method=account.auth_method,
        oauth_provider=cast("Literal['gmail'] | None", account.oauth_provider),
        folder_allow=account.folder_allow or [],
        folder_deny=account.folder_deny or [],
        folder_deny_flags=account.folder_deny_flags or [],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon_accounts.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/daemon_accounts.py tests/test_daemon_accounts.py
git commit -m "feat(daemon): pure DB-row -> AccountConfig adapter (Sub-plan 2A.2b)"
```

---

## Task 2: `list_syncable_accounts` query

**Files:**
- Modify: `src/localmail/api/admin/accounts.py` (after `list_accounts_full`, ~line 98)
- Test: `tests/test_admin_accounts.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_admin_accounts.py` (and add `list_syncable_accounts` to the existing import block from `localmail.api.admin.accounts`):

```python
def test_list_syncable_accounts_excludes_archive_and_disabled(db_conn):
    pw = _insert_account(db_conn, name='pw')
    oauth = _insert_account(db_conn, name='oauth', method='oauth2',
                            host='imap.gmail.com', oauth_provider='gmail')
    # archive: check constraint requires NULL host/port
    _insert_account(db_conn, name='arch', method='archive', host=None, port=None)
    off = _insert_account(db_conn, name='off')
    update_account(db_conn, off, sync_enabled=False)
    db_conn.commit()

    rows = list_syncable_accounts(db_conn)

    assert [r.name for r in rows] == ['pw', 'oauth']
    assert [r.id for r in rows] == [pw, oauth]
    assert all(isinstance(r, Account) for r in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py::test_list_syncable_accounts_excludes_archive_and_disabled -q`
Expected: FAIL — `ImportError: cannot import name 'list_syncable_accounts'`.

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/api/admin/accounts.py`, add directly after `list_accounts_full` (the function ending at ~line 97):

```python
def list_syncable_accounts(conn: psycopg.Connection) -> list[Account]:
    """Return live (non-archive), sync-enabled accounts, oldest first.

    This is the daemon's account source: archive accounts have no IMAP host
    and `sync_enabled = FALSE` accounts are paused, so neither spawns workers.
    Shares `_SELECT_FULL` with `get_account` so the column shape can't drift.
    """
    with conn.cursor(row_factory=class_row(Account)) as cur:
        cur.execute(
            _SELECT_FULL
            + " WHERE auth_method IN ('password', 'oauth2') AND sync_enabled"
            + " ORDER BY id"
        )
        return cur.fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py::test_list_syncable_accounts_excludes_archive_and_disabled -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/accounts.py tests/test_admin_accounts.py
git commit -m "feat(admin): list_syncable_accounts — daemon account source (Sub-plan 2A.2b)"
```

---

## Task 3: Neuter the `upsert_account` config overwrite

**Files:**
- Modify: `src/localmail/sync.py:63-89` (`upsert_account`)
- Test: `tests/test_sync.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sync.py` (add imports at the top of the file if not already present: `from localmail.sync import upsert_account` and `from localmail.config import AccountConfig`, and `from localmail.api.admin.accounts import create_account, get_account`):

```python
def test_upsert_account_does_not_overwrite_canonical_columns(db_conn):
    """DB is canonical: upsert_account is now get-or-create, never overwrite."""
    created = create_account(
        db_conn, name="acct", email_address="orig@example.com",
        auth_method="password", imap_host="orig.example.com", imap_port=993,
        oauth_provider=None, folder_allow=None, folder_deny=None,
        folder_deny_flags=None,
    )
    db_conn.commit()

    drift = AccountConfig(
        name="acct", email="drift@example.com",
        imap_host="drift.example.com", imap_port=143, auth_method="password",
    )
    returned_id = upsert_account(db_conn, drift)
    db_conn.commit()

    assert returned_id == created.id
    row = get_account(db_conn, created.id)
    assert row.email_address == "orig@example.com"
    assert row.imap_host == "orig.example.com"
    assert row.imap_port == 993


def test_upsert_account_inserts_brand_new_name(db_conn):
    acct = AccountConfig(
        name="fresh", email="f@example.com",
        imap_host="h.example.com", imap_port=993, auth_method="password",
    )
    new_id = upsert_account(db_conn, acct)
    db_conn.commit()
    assert isinstance(new_id, int)
    assert get_account(db_conn, new_id).name == "fresh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_sync.py::test_upsert_account_does_not_overwrite_canonical_columns -q`
Expected: FAIL — current `ON CONFLICT DO UPDATE` overwrites, so `email_address` becomes `"drift@example.com"`.

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/sync.py`, replace the `ON CONFLICT` clause in `upsert_account` (lines ~66-77). Change from:

```python
            INSERT INTO accounts
                (name, email_address, imap_host, imap_port, auth_method, oauth_provider)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                email_address  = EXCLUDED.email_address,
                imap_host      = EXCLUDED.imap_host,
                imap_port      = EXCLUDED.imap_port,
                auth_method    = EXCLUDED.auth_method,
                oauth_provider = EXCLUDED.oauth_provider
            RETURNING id
```

to:

```python
            INSERT INTO accounts
                (name, email_address, imap_host, imap_port, auth_method, oauth_provider)
            VALUES (%s, %s, %s, %s, %s, %s)
            -- DB is canonical for accounts (Sub-plan 2A.2b): get-or-create only.
            -- The no-op SET makes RETURNING fire for the existing row on
            -- conflict (DO NOTHING would return nothing) without touching any
            -- canonical column.
            ON CONFLICT (name) DO UPDATE SET name = accounts.name
            RETURNING id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_sync.py::test_upsert_account_does_not_overwrite_canonical_columns tests/test_sync.py::test_upsert_account_inserts_brand_new_name -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/sync.py tests/test_sync.py
git commit -m "fix(sync): upsert_account is get-or-create, no longer overwrites canonical columns (Sub-plan 2A.2b)"
```

---

## Task 4: `WorkerContext.account_id` + daemon enumerates the DB

**Files:**
- Modify: `src/localmail/worker.py`
- Modify: `src/localmail/daemon.py`
- Modify: `tests/test_daemon.py` (`make_ctx`, inline ctx at ~line 211)
- Modify: `tests/test_daemon_pool.py`

> At the end of this task, `idle.py`/`poller.py` STILL call `upsert_account(conn, ctx.account)` — which still works (get-or-create returns the existing id). Task 5 swaps them to `ctx.account_id`. Splitting this way keeps each task green.

- [ ] **Step 1: Add `account_id` to `WorkerContext`**

In `src/localmail/worker.py`, add the field immediately after `account`:

```python
@dataclass
class WorkerContext:
    account: AccountConfig
    account_id: int
    pool: ConnectionPool
    attachments_root: Path
    idle_renew_seconds: int
    poll_seconds: int
    gmail_client_secrets: Path | None
    stop: threading.Event
    ssl: bool = True
```

- [ ] **Step 2: Update `tests/test_daemon.py` so existing constructions still build**

Replace `make_ctx` (lines ~36-46) with a version that ensures the account row exists and carries its id:

```python
def make_ctx(
    pool: ConnectionPool,
    tmp_path: Path,
    stop: threading.Event,
    *,
    account: AccountConfig | None = None,
) -> WorkerContext:
    account = account or make_account()
    with pool.connection() as conn:
        account_id = upsert_account(conn, account)
        conn.commit()
    return WorkerContext(
        account=account,
        account_id=account_id,
        pool=pool,
        attachments_root=tmp_path,
        idle_renew_seconds=60,
        poll_seconds=1,
        gmail_client_secrets=None,
        stop=stop,
        ssl=False,
    )
```

Replace the inline `WorkerContext(...)` in `test_one_poll_pass_respects_folder_deny_flags` (lines ~207-215) with a `make_ctx` call that passes the custom account:

```python
    account = AccountConfig(
        name="acct", email="me@example.com", imap_host="imap.example.com",
        auth_method="password", folder_deny_flags=["\\Trash"],
    )
    ctx = make_ctx(pool, tmp_path, threading.Event(), account=account)
```

- [ ] **Step 3: Make the daemon enumerate the DB at construction time**

In `src/localmail/daemon.py`, update the imports:

```python
import logging
import signal
import threading
from typing import Any

import psycopg

from .api.admin.accounts import Account, list_syncable_accounts
from .config import Config
from .daemon_accounts import account_config_from_row
from .db import compute_daemon_pool_size, open_pool
from .idle import run_inbox_idle_loop
from .poller import run_poll_loop
from .worker import WorkerContext
```

Replace the body of `__init__` (lines ~20-61) so the account set comes from the DB and drives pool sizing:

```python
    def __init__(
        self,
        cfg: Config,
        *,
        ssl: bool = True,
        dsn: str | None = None,
        embedding_backend_factory=None,
    ) -> None:
        self.cfg = cfg
        self.ssl = ssl
        self._dsn = dsn or cfg.database.dsn
        self._stop_event = threading.Event()
        self._syncable = self._load_syncable_accounts()
        n_accounts = len(self._syncable)
        configured_max_size = cfg.daemon.pool_max_size
        if configured_max_size is None:
            resolved_max_size = compute_daemon_pool_size(
                n_accounts=n_accounts,
                run_embed=cfg.search.run_embed_worker,
                run_extract=cfg.search.run_extract_worker,
            )
        else:
            resolved_max_size = configured_max_size
        resolved_min_size = min(
            n_accounts * 2
            + (1 if cfg.search.run_embed_worker else 0)
            + (1 if cfg.search.run_extract_worker else 0)
            or 1,
            resolved_max_size,
        )
        log.info(
            "daemon pool sizing: max_size=%d min_size=%d (accounts=%d, embed=%s, extract=%s)",
            resolved_max_size,
            resolved_min_size,
            n_accounts,
            cfg.search.run_embed_worker,
            cfg.search.run_extract_worker,
        )
        self.pool = open_pool(
            self._dsn, min_size=resolved_min_size, max_size=resolved_max_size
        )
        self.threads: list[threading.Thread] = []
        self._embedding_backend_factory = embedding_backend_factory
        self._started = False

    def _load_syncable_accounts(self) -> list[Account]:
        """Enumerate live, sync-enabled accounts from the DB (one-shot conn).

        Done before the pool opens because pool sizing depends on the count.
        """
        with psycopg.connect(self._dsn) as conn:
            return list_syncable_accounts(conn)
```

Replace the account loop in `start_workers` (lines ~74-100) to build contexts from DB rows:

```python
        for account_row in self._syncable:
            ctx = WorkerContext(
                account=account_config_from_row(account_row),
                account_id=account_row.id,
                pool=self.pool,
                attachments_root=self.cfg.attachments.root,
                idle_renew_seconds=self.cfg.daemon.idle_renew_seconds,
                poll_seconds=self.cfg.daemon.poll_seconds,
                gmail_client_secrets=gmail_secrets,
                stop=self._stop_event,
                ssl=self.ssl,
            )
            t_idle = threading.Thread(
                target=run_inbox_idle_loop,
                args=(ctx,),
                name=f"idle-{account_row.name}",
                daemon=True,
            )
            t_poll = threading.Thread(
                target=run_poll_loop,
                args=(ctx,),
                name=f"poll-{account_row.name}",
                daemon=True,
            )
            t_idle.start()
            t_poll.start()
            self.threads += [t_idle, t_poll]
            log.info("started workers for %s", account_row.name)
```

In `run_forever` (line ~160), change the empty-guard:

```python
        if not self._syncable:
            log.warning("no syncable accounts in the DB; daemon exiting")
            return
```

- [ ] **Step 4: Guard the empty-table assumption in the existing pool test**

In `tests/test_daemon_pool.py`, the `test_daemon_pool_max_size_auto_computed` test asserts sizing for `n_accounts=0`. Add the `db_conn` fixture parameter so the accounts table is truncated+committed before `Daemon` reads it (defends against rows committed by an earlier test in the same run):

```python
def test_daemon_pool_max_size_auto_computed(db_dsn, db_conn) -> None:
    """No override → daemon picks the compute_daemon_pool_size value.

    db_conn truncates accounts so the DB-backed enumeration sees zero.
    """
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = True
    cfg.search.run_extract_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _FakeBackend())
    try:
        expected = compute_daemon_pool_size(
            n_accounts=0, run_embed=True, run_extract=True
        )
        assert d.pool.max_size == expected
    finally:
        d.pool.close()
```

- [ ] **Step 5: Add daemon-enumeration tests**

Append to `tests/test_daemon_pool.py` (add to the imports: `from localmail.api.admin.accounts import create_account, update_account`):

```python
def _seed_two_syncable_plus_noise(conn) -> None:
    create_account(conn, name="pw", email_address="a@x.com",
                   auth_method="password", imap_host="h", imap_port=993,
                   oauth_provider=None, folder_allow=None, folder_deny=None,
                   folder_deny_flags=None)
    create_account(conn, name="oauth", email_address="b@x.com",
                   auth_method="oauth2", imap_host="h", imap_port=993,
                   oauth_provider="gmail", folder_allow=None, folder_deny=None,
                   folder_deny_flags=None)
    create_account(conn, name="arch", email_address="c@x.com",
                   auth_method="archive", imap_host=None, imap_port=None,
                   oauth_provider=None, folder_allow=None, folder_deny=None,
                   folder_deny_flags=None)
    off = create_account(conn, name="off", email_address="d@x.com",
                         auth_method="password", imap_host="h", imap_port=993,
                         oauth_provider=None, folder_allow=None,
                         folder_deny=None, folder_deny_flags=None)
    update_account(conn, off.id, sync_enabled=False)
    conn.commit()


def test_daemon_syncable_excludes_archive_and_disabled(db_dsn, db_conn) -> None:
    _seed_two_syncable_plus_noise(db_conn)
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _FakeBackend())
    try:
        assert [r.name for r in d._syncable] == ["pw", "oauth"]
    finally:
        d.pool.close()


def test_daemon_pool_sizes_from_db_account_count(db_dsn, db_conn) -> None:
    _seed_two_syncable_plus_noise(db_conn)
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _FakeBackend())
    try:
        assert d.pool.max_size == compute_daemon_pool_size(
            n_accounts=2, run_embed=False, run_extract=False
        )
    finally:
        d.pool.close()
```

- [ ] **Step 6: Run the affected tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon.py tests/test_daemon_pool.py -q`
Expected: PASS (all existing daemon + pool tests, plus the 2 new enumeration tests).

- [ ] **Step 7: Commit**

```bash
git add src/localmail/worker.py src/localmail/daemon.py tests/test_daemon.py tests/test_daemon_pool.py
git commit -m "feat(daemon): enumerate accounts from the DB; carry account_id on WorkerContext (Sub-plan 2A.2b)"
```

---

## Task 5: Workers read `ctx.account_id` (drop `upsert_account`)

**Files:**
- Modify: `src/localmail/idle.py` (`_ensure_inbox_row`, import)
- Modify: `src/localmail/poller.py` (`_one_poll_pass`, import)
- Test: `tests/test_daemon.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_daemon.py`:

```python
def test_one_poll_pass_does_not_call_upsert_account(pool, tmp_path: Path, monkeypatch):
    """The daemon already has account_id from the DB; the poll pass must not
    re-upsert the account row (would re-introduce the canonical overwrite)."""
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.add_folder("Archive")
    imap.append("Archive", _eml.plain())

    @contextmanager
    def fake_open(account, **kw):  # noqa: ARG001
        yield imap

    monkeypatch.setattr(poll_mod, "open_connection", fake_open)

    calls = {"n": 0}
    real = poll_mod.upsert_account if hasattr(poll_mod, "upsert_account") else None

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    if real is not None:
        monkeypatch.setattr(poll_mod, "upsert_account", spy)

    ctx = make_ctx(pool, tmp_path, threading.Event())
    _one_poll_pass(ctx)

    assert calls["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon.py::test_one_poll_pass_does_not_call_upsert_account -q`
Expected: FAIL — `_one_poll_pass` currently calls `upsert_account` once (`calls["n"] == 1`).

- [ ] **Step 3: Update `poller.py`**

In `src/localmail/poller.py`, change the import (line 9) to drop `upsert_account`:

```python
from .sync import folders_to_sync, sync_mailbox, upsert_mailbox
```

Replace the account block in `_one_poll_pass` (lines ~43-45):

```python
        with ctx.pool.connection() as conn:
            account_id = upsert_account(conn, ctx.account)
            conn.commit()
```

with:

```python
        account_id = ctx.account_id
```

- [ ] **Step 4: Update `idle.py`**

In `src/localmail/idle.py`, change the import (line 16) to drop `upsert_account`:

```python
from .sync import sync_mailbox, upsert_mailbox
```

Replace `_ensure_inbox_row` (lines ~91-98):

```python
def _ensure_inbox_row(ctx: WorkerContext):
    with ctx.pool.connection() as conn:
        mailbox = upsert_mailbox(
            conn, account_id=ctx.account_id, name=INBOX, delimiter=None, flags=[]
        )
        conn.commit()
    return ctx.account_id, mailbox
```

- [ ] **Step 5: Run the affected tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_daemon.py -q`
Expected: PASS (all daemon tests, including the new no-upsert assertion).

- [ ] **Step 6: Commit**

```bash
git add src/localmail/idle.py src/localmail/poller.py tests/test_daemon.py
git commit -m "refactor(daemon): workers use ctx.account_id instead of upsert_account (Sub-plan 2A.2b)"
```

---

## Task 6: Docs + full verification

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the CLAUDE.md "DB canonical" note**

In `CLAUDE.md`, find the paragraph under the TOML→DB seed bullet ending with:

> Note `sync.py:upsert_account` still overwrites `email/host/port/auth_method/oauth_provider` from config on first sync, so the DB is not yet *fully* canonical against the running daemon until the daemon-source slice lands.

Replace it with:

> The daemon now reads its account set from the `accounts` table
> (`api.admin.accounts.list_syncable_accounts` → `daemon_accounts.account_config_from_row`),
> skips `sync_enabled = FALSE` and `archive` accounts, and carries the DB
> `account_id` on `WorkerContext` so the IDLE/poll workers no longer call
> `upsert_account`. `sync.py:upsert_account` is now get-or-create only (no
> canonical-column overwrite) and survives solely for the still-TOML-driven
> one-shot `localmail sync` CLI (rewiring is 2A.2d). Per-account
> `poll_seconds` TOML overrides are no longer honored by the daemon (no DB
> column); the daemon-wide `cfg.daemon.poll_seconds` applies to every account.

- [ ] **Step 2: Run the full suite**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/`
Expected: PASS — full suite green (998 prior + the new adapter/query/sync/daemon tests).

- [ ] **Step 3: Run mypy**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: `Success: no issues found`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): daemon reads accounts from the DB; retire risk #2 note (Sub-plan 2A.2b)"
```

---

## Self-review notes

- **Spec coverage:** Task 1 = adapter; Task 2 = `list_syncable_accounts` (live + `sync_enabled`, archive excluded); Task 3 = `upsert_account` neutering (risk #2); Task 4 = `WorkerContext.account_id` + daemon enumeration + pool sizing from DB count; Task 5 = workers use `ctx.account_id`; Task 6 = docs + behavioral-delta note + verification. All spec sections covered.
- **Type consistency:** `account_config_from_row(Account) -> AccountConfig`, `list_syncable_accounts(conn) -> list[Account]`, `WorkerContext.account_id: int`, `Daemon._syncable: list[Account]`, `Daemon._load_syncable_accounts() -> list[Account]` — names/signatures used consistently across tasks.
- **No placeholders:** every code-changing step shows the exact code and command.
