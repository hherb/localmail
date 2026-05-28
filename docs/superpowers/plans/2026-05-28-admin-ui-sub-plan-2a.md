# Admin UI Sub-plan 2A — Account Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the admin-UI account-management surface from the design doc § 2A: schema migration 0020, accounts CRUD service layer, HMAC-signed-state Gmail web OAuth flow, and nine HTTP routes — in a single PR. Closes **#114** (state_signing_key consumer wired up).

**Architecture:** Two new service-layer modules under `src/localmail/api/admin/` (`accounts.py`, `oauth.py`) plus a small HMAC helper (`oauth_state.py`). Two new HTTP routers under `src/localmail/serve/admin/` (`accounts_router.py`, `oauth_router.py`). New migration `0020_accounts_canonical.sql`. Re-uses the existing `AdminUser` + `require_admin_session` + CSRF + Postgres pool + keyring infrastructure. The OAuth state token is the stateless format from the design doc: `base64url(json(payload)) + "." + base64url(hmac_sha256(key, base64url(json(payload))))`.

**Tech Stack:** Python 3.12 + `psycopg` v3 + raw SQL, FastAPI, Jinja2 (admin templates pre-exist), `pydantic` v2, `imapclient`, `google-auth-oauthlib` (existing OAuth dep), `keyring`, `pytest`.

**Out of scope (deferred):**
- Existing CLI rewiring (`add-account`, `oauth-login`, `remove-account` keep their TOML-only behavior) — that's Sub-plan **2A.2**.
- Jinja2/HTMX UI screens for accounts — that's Sub-plan **2A.3** (the design doc § 4 templates).
- TOML→DB seed at `init-db` — deferred to 2A.2 alongside the CLI rewiring.

---

## Pre-flight

- [ ] **Confirm state.** On `main` at `0f0a96e`, working tree clean
  (`.claude/settings.local.json` untracked is fine), `uv run pytest -q tests/`
  passes (last green: **909 passed**).
- [ ] **Create worktree.** Use `superpowers:using-git-worktrees` so the
  feature work is isolated from `main`. Branch name:
  `sub-plan-2a-account-management`.

---

## Task 1: Migration `0020_accounts_canonical.sql`

**Files:**
- Create: `migrations/0020_accounts_canonical.sql`
- Test: `tests/test_migration_0020.py` (new)

The existing `accounts` table (per `0001_init.sql`) already has
`name`, `email_address`, `imap_host`, `imap_port`, `auth_method`,
`oauth_provider`, `config` (JSONB), `created_at`. So 0020 only
adds the *new* columns (folder filters, sync_enabled, updated_at),
extends the `auth_method` check to accept `'archive'`, lifts the
existing NOT NULL on `imap_host` / `imap_port` so archive accounts
can have NULL there, and adds the
`accounts_live_requires_host` invariant.

- [ ] **Step 1: Write the migration**

```sql
-- 0020_accounts_canonical.sql
-- Promote the accounts table to be authoritative (DB-canonical), as
-- planned by the admin UI design doc (2026-05-28).
--
-- This migration is intentionally idempotent (every ALTER uses
-- IF (NOT) EXISTS-style guards) so re-running on a partially-migrated
-- archive is safe.

BEGIN;

-- Folder-filter columns (currently held in config.toml).
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS folder_allow      JSONB;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS folder_deny       JSONB;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS folder_deny_flags JSONB;

-- v1.x reservation: per-account sync pause. Daemon does NOT honor it yet.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS sync_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- Audit timestamp for the admin UI.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Allow the 'archive' auth method (mbox import lands in v1's Sub-plan 2C).
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_auth_method_check;
ALTER TABLE accounts ADD  CONSTRAINT accounts_auth_method_check
  CHECK (auth_method IN ('password', 'oauth2', 'archive'));

-- Live IMAP accounts must have host + port; archive accounts must not.
-- Lift the legacy NOT NULL on imap_host / imap_port first so 'archive'
-- accounts can NULL them.
ALTER TABLE accounts ALTER COLUMN imap_host DROP NOT NULL;
ALTER TABLE accounts ALTER COLUMN imap_port DROP NOT NULL;

ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_live_requires_host;
ALTER TABLE accounts ADD  CONSTRAINT accounts_live_requires_host
  CHECK (
    (auth_method = 'archive'
      AND imap_host IS NULL AND imap_port IS NULL)
    OR
    (auth_method IN ('password', 'oauth2')
      AND imap_host IS NOT NULL AND imap_port IS NOT NULL)
  );

COMMIT;
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_migration_0020.py
"""Regression tests for the 0020_accounts_canonical migration.

Exercises the post-migration shape against the real test DB (the migration
has already applied via the conftest db_conn fixture, which TRUNCATEs).
"""

import pytest


def test_folder_filter_columns_exist(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name
              FROM information_schema.columns
             WHERE table_name = 'accounts'
               AND column_name IN ('folder_allow', 'folder_deny',
                                   'folder_deny_flags', 'sync_enabled',
                                   'updated_at')
        """)
        present = {row[0] for row in cur.fetchall()}
    assert present == {
        'folder_allow', 'folder_deny', 'folder_deny_flags',
        'sync_enabled', 'updated_at',
    }


def test_archive_auth_method_is_accepted(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method) "
            "VALUES ('arch', 'a@b.test', 'archive') RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None and row[0] > 0


def test_archive_accounts_cannot_have_host(db_conn):
    import psycopg
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port) "
            "VALUES ('arch2', 'a@b.test', 'archive', 'imap.example', 993)"
        )


def test_live_accounts_must_have_host(db_conn):
    import psycopg
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method) "
            "VALUES ('broken', 'a@b.test', 'password')"
        )
```

- [ ] **Step 3: Run tests to verify they fail**

```
unset VIRTUAL_ENV && uv run pytest -q tests/test_migration_0020.py
```

Expected: 4 failures (`folder_allow` column missing, etc.) because the
migration file does not exist yet.

- [ ] **Step 4: Apply the migration**

The migration runs automatically on the next `db_conn` fixture call (the
conftest applies pending migrations before TRUNCATE). To verify directly:

```
unset VIRTUAL_ENV && uv run localmail init-db
```

- [ ] **Step 5: Run tests to verify they pass**

```
unset VIRTUAL_ENV && uv run pytest -q tests/test_migration_0020.py
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```
git add migrations/0020_accounts_canonical.sql tests/test_migration_0020.py
git commit -m "feat(admin): migration 0020 — folder filters + archive auth_method on accounts"
```

---

## Task 2: Service layer — dataclasses + read paths (`list_accounts`, `get_account`)

**Files:**
- Create: `src/localmail/api/admin/accounts.py`
- Test: `tests/test_admin_accounts.py`

The existing accounts table column for email is `email_address` (not `email`
as the design doc § 2A pseudo-code suggests). The service layer keeps the
existing column name internally and exposes a `Account.email_address` field
on the dataclass.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_accounts.py
"""Service-layer tests for localmail.api.admin.accounts."""

import pytest

from localmail.api.admin.accounts import (
    Account, AccountSummary,
    list_accounts, get_account,
)
from localmail.api.errors import NotFound


def _insert_account(conn, *, name, email='x@y.test', method='password',
                    host='imap.example', port=993, oauth_provider=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, oauth_provider, config) "
            "VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb) RETURNING id",
            (name, email, method, host, port, oauth_provider),
        )
        return cur.fetchone()[0]


def test_list_accounts_returns_summaries_in_id_order(db_conn):
    id_a = _insert_account(db_conn, name='alpha')
    id_b = _insert_account(db_conn, name='beta')
    summaries = list_accounts(db_conn)
    assert [s.id for s in summaries] == [id_a, id_b]
    assert all(isinstance(s, AccountSummary) for s in summaries)
    assert summaries[0].name == 'alpha'
    assert summaries[0].auth_method == 'password'


def test_get_account_returns_full_record(db_conn):
    aid = _insert_account(db_conn, name='gamma',
                          email='g@example.test', method='oauth2',
                          host='imap.gmail.com', port=993,
                          oauth_provider='gmail')
    acct = get_account(db_conn, aid)
    assert isinstance(acct, Account)
    assert acct.id == aid
    assert acct.name == 'gamma'
    assert acct.email_address == 'g@example.test'
    assert acct.auth_method == 'oauth2'
    assert acct.oauth_provider == 'gmail'
    assert acct.imap_host == 'imap.gmail.com'
    assert acct.imap_port == 993
    assert acct.sync_enabled is True
    assert acct.folder_allow is None
    assert acct.folder_deny is None
    assert acct.folder_deny_flags is None


def test_get_account_missing_raises_not_found(db_conn):
    with pytest.raises(NotFound):
        get_account(db_conn, 9999)
```

- [ ] **Step 2: Run to verify they fail**

```
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_accounts.py
```

Expected: ImportError on `localmail.api.admin.accounts`.

- [ ] **Step 3: Implement**

```python
# src/localmail/api/admin/accounts.py
"""Service layer for admin-UI account management (Sub-plan 2A).

Pure functions over a psycopg connection. No FastAPI imports; no IO beyond
the connection passed in. Field validation is shared with the daemon via
the AccountConfig model in localmail.config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg

from localmail.api.errors import NotFound


AuthMethod = Literal['password', 'oauth2', 'archive']


@dataclass(frozen=True)
class AccountSummary:
    id: int
    name: str
    email_address: str | None
    auth_method: AuthMethod
    sync_enabled: bool


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    email_address: str | None
    auth_method: AuthMethod
    oauth_provider: str | None
    imap_host: str | None
    imap_port: int | None
    folder_allow: list[str] | None
    folder_deny: list[str] | None
    folder_deny_flags: list[str] | None
    sync_enabled: bool
    created_at: datetime
    updated_at: datetime


_SELECT_FULL = """
    SELECT id, name, email_address, auth_method, oauth_provider,
           imap_host, imap_port,
           folder_allow, folder_deny, folder_deny_flags,
           sync_enabled, created_at, updated_at
      FROM accounts
"""


def list_accounts(conn: psycopg.Connection) -> list[AccountSummary]:
    """Return every configured account, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, email_address, auth_method, sync_enabled "
            "FROM accounts ORDER BY id"
        )
        return [AccountSummary(*row) for row in cur.fetchall()]


def get_account(conn: psycopg.Connection, account_id: int) -> Account:
    """Return one account by id. Raises NotFound if absent."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_FULL + " WHERE id = %s", (account_id,))
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"account {account_id} not found")
    return Account(*row)
```

- [ ] **Step 4: Run tests to verify they pass**

```
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_accounts.py
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add src/localmail/api/admin/accounts.py tests/test_admin_accounts.py
git commit -m "feat(admin): accounts service — Account dataclass + list/get"
```

---

## Task 3: Service layer — `create_account` + `update_account`

**Files:**
- Modify: `src/localmail/api/admin/accounts.py`
- Modify: `tests/test_admin_accounts.py`

Field validation goes through a new helper `_validate_account_fields` that
re-uses the same regexes/constraints as `AccountConfig` in `config.py`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_admin_accounts.py
from localmail.api.admin.accounts import (
    create_account, update_account, AccountFieldError,
)


def test_create_account_password_round_trip(db_conn):
    acct = create_account(
        db_conn,
        name='work',
        email_address='work@example.test',
        auth_method='password',
        imap_host='imap.example',
        imap_port=993,
        oauth_provider=None,
        folder_allow=None,
        folder_deny=['Spam'],
        folder_deny_flags=['\\Junk'],
    )
    assert acct.id > 0 and acct.name == 'work'
    assert acct.folder_deny == ['Spam']
    fetched = get_account(db_conn, acct.id)
    assert fetched == acct


def test_create_account_archive_has_null_host(db_conn):
    acct = create_account(
        db_conn,
        name='legacy-2017',
        email_address=None,
        auth_method='archive',
        imap_host=None,
        imap_port=None,
        oauth_provider=None,
        folder_allow=None,
        folder_deny=None,
        folder_deny_flags=None,
    )
    assert acct.auth_method == 'archive'
    assert acct.imap_host is None and acct.imap_port is None


def test_create_account_rejects_blank_name(db_conn):
    with pytest.raises(AccountFieldError):
        create_account(
            db_conn,
            name='',
            email_address='x@y.test',
            auth_method='password',
            imap_host='h', imap_port=993,
            oauth_provider=None,
            folder_allow=None, folder_deny=None, folder_deny_flags=None,
        )


def test_create_account_rejects_password_without_host(db_conn):
    with pytest.raises(AccountFieldError):
        create_account(
            db_conn,
            name='x',
            email_address='x@y.test',
            auth_method='password',
            imap_host=None, imap_port=None,
            oauth_provider=None,
            folder_allow=None, folder_deny=None, folder_deny_flags=None,
        )


def test_update_account_changes_folders_and_bumps_updated_at(db_conn):
    aid = _insert_account(db_conn, name='u')
    before = get_account(db_conn, aid)
    updated = update_account(
        db_conn,
        aid,
        folder_deny=['Trash', 'Bin'],
        sync_enabled=False,
    )
    assert updated.folder_deny == ['Trash', 'Bin']
    assert updated.sync_enabled is False
    assert updated.updated_at >= before.updated_at


def test_update_account_missing_raises_not_found(db_conn):
    with pytest.raises(NotFound):
        update_account(db_conn, 9999, sync_enabled=False)


def test_update_account_rejects_changing_auth_method_to_archive_with_host(db_conn):
    aid = _insert_account(db_conn, name='live')
    with pytest.raises(AccountFieldError):
        update_account(db_conn, aid, auth_method='archive')
```

- [ ] **Step 2: Run to verify they fail**

```
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_accounts.py
```

Expected: ImportError on the new symbols.

- [ ] **Step 3: Implement**

```python
# append to src/localmail/api/admin/accounts.py

class AccountFieldError(ValueError):
    """Raised when field validation rejects a create/update."""


_NAME_MAX = 128
_HOSTNAME_MAX = 255
_PORT_MIN = 1
_PORT_MAX = 65535


def _validate_create_fields(*, name, email_address, auth_method,
                            imap_host, imap_port, oauth_provider) -> None:
    if not name or not name.strip():
        raise AccountFieldError("name must not be blank")
    if len(name) > _NAME_MAX:
        raise AccountFieldError(f"name longer than {_NAME_MAX} chars")
    if auth_method not in ('password', 'oauth2', 'archive'):
        raise AccountFieldError(f"unknown auth_method {auth_method!r}")
    if auth_method == 'archive':
        if imap_host is not None or imap_port is not None:
            raise AccountFieldError(
                "archive accounts must not have imap_host/imap_port"
            )
    else:
        if not imap_host:
            raise AccountFieldError("imap_host required for live accounts")
        if len(imap_host) > _HOSTNAME_MAX:
            raise AccountFieldError("imap_host too long")
        if imap_port is None or not (_PORT_MIN <= imap_port <= _PORT_MAX):
            raise AccountFieldError("imap_port required and in 1..65535")
    if auth_method == 'oauth2' and oauth_provider not in ('gmail',):
        raise AccountFieldError(
            "oauth2 accounts require oauth_provider='gmail'"
        )


def _validate_update_field_combo(*, current_method, new_method,
                                 imap_host, imap_port) -> None:
    method = new_method or current_method
    if method == 'archive' and (imap_host is not None or imap_port is not None):
        raise AccountFieldError(
            "archive accounts must not have imap_host/imap_port"
        )


def create_account(
    conn: psycopg.Connection,
    *,
    name: str,
    email_address: str | None,
    auth_method: AuthMethod,
    imap_host: str | None,
    imap_port: int | None,
    oauth_provider: str | None,
    folder_allow: list[str] | None,
    folder_deny: list[str] | None,
    folder_deny_flags: list[str] | None,
) -> Account:
    """Insert a new account row and return it."""
    _validate_create_fields(
        name=name, email_address=email_address, auth_method=auth_method,
        imap_host=imap_host, imap_port=imap_port,
        oauth_provider=oauth_provider,
    )
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO accounts (name, email_address, auth_method, "
                "  oauth_provider, imap_host, imap_port, "
                "  folder_allow, folder_deny, folder_deny_flags, config) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb) "
                "RETURNING id",
                (
                    name, email_address, auth_method, oauth_provider,
                    imap_host, imap_port,
                    psycopg.types.json.Jsonb(folder_allow) if folder_allow is not None else None,
                    psycopg.types.json.Jsonb(folder_deny)  if folder_deny  is not None else None,
                    psycopg.types.json.Jsonb(folder_deny_flags) if folder_deny_flags is not None else None,
                ),
            )
        except psycopg.errors.UniqueViolation as e:
            raise AccountFieldError(f"account name {name!r} already exists") from e
        new_id = cur.fetchone()[0]
    return get_account(conn, new_id)


_UPDATABLE = {
    'email_address', 'auth_method', 'oauth_provider',
    'imap_host', 'imap_port',
    'folder_allow', 'folder_deny', 'folder_deny_flags',
    'sync_enabled',
}


def update_account(conn: psycopg.Connection, account_id: int, **fields) -> Account:
    """Partial-update an account. Bumps updated_at = now()."""
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise AccountFieldError(f"unknown fields: {sorted(unknown)}")
    if not fields:
        return get_account(conn, account_id)

    current = get_account(conn, account_id)
    _validate_update_field_combo(
        current_method=current.auth_method,
        new_method=fields.get('auth_method'),
        imap_host=fields.get('imap_host', current.imap_host),
        imap_port=fields.get('imap_port', current.imap_port),
    )

    set_sql_parts: list[str] = []
    values: list = []
    for key, val in fields.items():
        if key in ('folder_allow', 'folder_deny', 'folder_deny_flags'):
            set_sql_parts.append(f"{key} = %s")
            values.append(psycopg.types.json.Jsonb(val) if val is not None else None)
        else:
            set_sql_parts.append(f"{key} = %s")
            values.append(val)
    set_sql_parts.append("updated_at = now()")
    values.append(account_id)

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE accounts SET {', '.join(set_sql_parts)} WHERE id = %s",
            values,
        )
        if cur.rowcount == 0:
            raise NotFound(f"account {account_id} not found")
    return get_account(conn, account_id)
```

- [ ] **Step 4: Run tests**

```
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_accounts.py
```

Expected: 10 passed (3 from Task 2 + 7 new).

- [ ] **Step 5: Commit**

```
git add src/localmail/api/admin/accounts.py tests/test_admin_accounts.py
git commit -m "feat(admin): accounts service — create/update + field validation"
```

---

## Task 4: Service layer — `delete_account` + cascade behavior

**Files:**
- Modify: `src/localmail/api/admin/accounts.py`
- Modify: `tests/test_admin_accounts.py`

The design says: refuses when `messages` rows reference the account, unless
`force=True`. Cascade deletes through `message_labels`, `mailboxes`,
`failed_messages`, etc. Keyring secrets cleared atomically (best-effort).

The existing `accounts` FK from `messages` already cascades on delete (per
`0001_init.sql`), so the cascade itself is automatic. The new check is the
*refusal* when messages exist and `force=False`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_admin_accounts.py
from localmail.api.admin.accounts import delete_account, AccountInUse


def test_delete_empty_account_succeeds(db_conn):
    aid = _insert_account(db_conn, name='empty')
    delete_account(db_conn, aid)
    with pytest.raises(NotFound):
        get_account(db_conn, aid)


def test_delete_account_with_messages_refuses_without_force(db_conn):
    aid = _insert_account(db_conn, name='busy')
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mailboxes (account_id, name, uidvalidity, uidnext) "
            "VALUES (%s, 'INBOX', 1, 1) RETURNING id", (aid,))
        mbox_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, mailbox_id, uid, raw_bytes, "
            "raw_sha256, size_bytes, headers, attachments) "
            "VALUES (%s, %s, 1, %s, %s, %s, '{}'::jsonb, '[]'::jsonb)",
            (aid, mbox_id, b'x', 'a'*64, 1))
    with pytest.raises(AccountInUse):
        delete_account(db_conn, aid)


def test_delete_account_with_messages_force_cascades(db_conn):
    aid = _insert_account(db_conn, name='busy2')
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mailboxes (account_id, name, uidvalidity, uidnext) "
            "VALUES (%s, 'INBOX', 1, 1) RETURNING id", (aid,))
        mbox_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, mailbox_id, uid, raw_bytes, "
            "raw_sha256, size_bytes, headers, attachments) "
            "VALUES (%s, %s, 1, %s, %s, %s, '{}'::jsonb, '[]'::jsonb)",
            (aid, mbox_id, b'x', 'a'*64, 1))
    delete_account(db_conn, aid, force=True)
    with pytest.raises(NotFound):
        get_account(db_conn, aid)
```

- [ ] **Step 2: Run to verify they fail**

Expected: ImportError on `delete_account` / `AccountInUse`.

- [ ] **Step 3: Implement**

```python
# append to src/localmail/api/admin/accounts.py

class AccountInUse(RuntimeError):
    """Raised when delete is refused because messages reference the account."""


def delete_account(conn: psycopg.Connection, account_id: int,
                   *, force: bool = False) -> None:
    """Delete an account. Refuses if messages reference it unless force=True."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,))
        if cur.fetchone() is None:
            raise NotFound(f"account {account_id} not found")
        if not force:
            cur.execute(
                "SELECT 1 FROM messages WHERE account_id = %s LIMIT 1",
                (account_id,))
            if cur.fetchone() is not None:
                raise AccountInUse(
                    f"account {account_id} still has messages; pass force=True"
                )
        cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
```

- [ ] **Step 4: Run tests**

Expected: 13 passed (10 prior + 3 new).

- [ ] **Step 5: Commit**

```
git add src/localmail/api/admin/accounts.py tests/test_admin_accounts.py
git commit -m "feat(admin): accounts service — delete with cascade-or-refuse guard"
```

---

## Task 5: Service layer — `store_password` + `clear_secret`

**Files:**
- Modify: `src/localmail/api/admin/accounts.py`
- Modify: `tests/test_admin_accounts.py`

Existing `localmail.secrets` is the keyring wrapper; use it. The `memory_keyring`
autouse fixture in `tests/conftest.py` intercepts so tests don't touch real
Keychain.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_admin_accounts.py
import keyring
from localmail.api.admin.accounts import store_password, clear_secret


def test_store_password_writes_keyring(db_conn):
    aid = _insert_account(db_conn, name='kring')
    acct = get_account(db_conn, aid)
    store_password(acct, 'sekret')
    assert keyring.get_password('localmail', 'kring') == 'sekret'


def test_clear_secret_removes_keyring_entries(db_conn):
    aid = _insert_account(db_conn, name='kring2')
    acct = get_account(db_conn, aid)
    store_password(acct, 'sekret')
    keyring.set_password('localmail', 'kring2:refresh', 'refr')
    clear_secret(acct)
    assert keyring.get_password('localmail', 'kring2') is None
    assert keyring.get_password('localmail', 'kring2:refresh') is None


def test_clear_secret_tolerates_missing_keyring_entries(db_conn):
    aid = _insert_account(db_conn, name='kring3')
    acct = get_account(db_conn, aid)
    clear_secret(acct)  # no-op, no raise
```

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Implement**

```python
# append to src/localmail/api/admin/accounts.py
from localmail import secrets as _secrets


def store_password(account: Account, password: str) -> None:
    """Store an IMAP password for the given account in the keyring."""
    if account.auth_method != 'password':
        raise AccountFieldError(
            f"store_password requires auth_method='password', got "
            f"{account.auth_method!r}"
        )
    _secrets.set_password(account.name, password)


def clear_secret(account: Account) -> None:
    """Best-effort clear both the password and any refresh-token entries.

    Missing entries are tolerated — the operator-facing intent is
    "leave nothing behind", not "fail if nothing is there".
    """
    _secrets.delete_password(account.name)
    _secrets.delete_refresh_token(account.name)
```

(If `localmail.secrets` doesn't already expose those exact names,
inspect it and use whatever wrappers are there — the tests pin the
behavior, not the function names.)

- [ ] **Step 4: Run tests**

Expected: 16 passed.

- [ ] **Step 5: Commit**

```
git add src/localmail/api/admin/accounts.py tests/test_admin_accounts.py
git commit -m "feat(admin): accounts service — store_password / clear_secret"
```

---

## Task 6: Service layer — `test_connection` (IMAP folder probe)

**Files:**
- Modify: `src/localmail/api/admin/accounts.py`
- Modify: `tests/test_admin_accounts.py`
- Reuse: `tests/_fake_imap.py` (existing FakeIMAPClient)

The design says: opens an IMAP connection, lists folders, returns names.
Reuse `imap_client.open_connection()`; for tests, monkeypatch it to return
a `FakeIMAPClient`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_admin_accounts.py
from localmail.api.admin.accounts import test_connection, FolderInfo
from tests._fake_imap import FakeIMAPClient


def test_test_connection_returns_folder_list(db_conn, monkeypatch):
    aid = _insert_account(db_conn, name='tc')
    acct = get_account(db_conn, aid)

    fake = FakeIMAPClient.with_folders(['INBOX', '[Gmail]/All Mail', 'Sent'])

    def fake_open_connection(account, password=None):
        return fake

    monkeypatch.setattr(
        'localmail.api.admin.accounts._open_imap_connection',
        fake_open_connection,
    )
    folders = test_connection(db_conn, aid)
    assert [f.name for f in folders] == ['INBOX', '[Gmail]/All Mail', 'Sent']
    assert all(isinstance(f, FolderInfo) for f in folders)
```

(If `FakeIMAPClient.with_folders` doesn't exist yet, add a small constructor
to `_fake_imap.py` that pre-populates the in-memory folder set — a 5-line
helper.)

- [ ] **Step 2: Implement**

```python
# append to src/localmail/api/admin/accounts.py
from localmail import imap_client as _imap


@dataclass(frozen=True)
class FolderInfo:
    name: str
    flags: tuple[str, ...]


def _open_imap_connection(account, password=None):
    """Indirection so tests can monkeypatch."""
    return _imap.open_connection(account, password=password)


def test_connection(conn: psycopg.Connection, account_id: int) -> list[FolderInfo]:
    """Open IMAP, list folders, return summary. Raises on connect failure."""
    account = get_account(conn, account_id)
    if account.auth_method == 'archive':
        raise AccountFieldError("test_connection not applicable to archive accounts")
    password = _secrets.get_password(account.name) if account.auth_method == 'password' else None
    with _open_imap_connection(account, password=password) as client:
        listing = client.list_folders()
    return [FolderInfo(name=name, flags=tuple(flags)) for flags, _delim, name in listing]
```

- [ ] **Step 3: Run tests**

Expected: 17 passed.

- [ ] **Step 4: Commit**

```
git add src/localmail/api/admin/accounts.py tests/test_admin_accounts.py
git commit -m "feat(admin): accounts service — test_connection IMAP folder probe"
```

---

## Task 7: HMAC-signed OAuth state token helpers

**Files:**
- Create: `src/localmail/api/admin/oauth_state.py`
- Test: `tests/test_admin_oauth_state.py`

Format from the design doc:
`base64url(json(payload)) + "." + base64url(hmac_sha256(key, base64url(json(payload))))`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_oauth_state.py
"""Round-trip + tamper + expiry tests for the OAuth state token."""

import time
import pytest

from localmail.api.admin.oauth_state import (
    encode_state, decode_state,
    StatePayload, StateExpired, StateInvalid,
)


KEY = b"k" * 32


def test_encode_decode_roundtrip():
    payload = StatePayload(user_id=42, account_id=7,
                           nonce='abc', exp=int(time.time()) + 60)
    token = encode_state(payload, key=KEY)
    decoded = decode_state(token, key=KEY)
    assert decoded == payload


def test_decode_rejects_tampered_payload():
    payload = StatePayload(user_id=42, account_id=7,
                           nonce='abc', exp=int(time.time()) + 60)
    token = encode_state(payload, key=KEY)
    head, sig = token.split('.', 1)
    # Flip the last char of the payload half.
    bad_token = head[:-1] + ('A' if head[-1] != 'A' else 'B') + '.' + sig
    with pytest.raises(StateInvalid):
        decode_state(bad_token, key=KEY)


def test_decode_rejects_tampered_signature():
    payload = StatePayload(user_id=1, account_id=1,
                           nonce='a', exp=int(time.time()) + 60)
    token = encode_state(payload, key=KEY)
    head, sig = token.split('.', 1)
    bad_token = head + '.' + sig[:-1] + ('A' if sig[-1] != 'A' else 'B')
    with pytest.raises(StateInvalid):
        decode_state(bad_token, key=KEY)


def test_decode_rejects_wrong_key():
    payload = StatePayload(user_id=1, account_id=1,
                           nonce='a', exp=int(time.time()) + 60)
    token = encode_state(payload, key=KEY)
    with pytest.raises(StateInvalid):
        decode_state(token, key=b"x" * 32)


def test_decode_raises_state_expired_when_past_exp():
    payload = StatePayload(user_id=1, account_id=1,
                           nonce='a', exp=int(time.time()) - 1)
    token = encode_state(payload, key=KEY)
    with pytest.raises(StateExpired):
        decode_state(token, key=KEY)


def test_decode_rejects_malformed_token():
    with pytest.raises(StateInvalid):
        decode_state("no-dot-here", key=KEY)
```

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Implement**

```python
# src/localmail/api/admin/oauth_state.py
"""Stateless HMAC-signed OAuth state tokens for the admin web flow.

Format: base64url(json(payload)) + "." + base64url(hmac_sha256(key, payload_b64)).
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import asdict, dataclass
from hashlib import sha256


@dataclass(frozen=True)
class StatePayload:
    user_id: int
    account_id: int
    nonce: str
    exp: int


class StateExpired(ValueError):
    """Token signed correctly but its exp is in the past."""


class StateInvalid(ValueError):
    """Token shape, signature, or payload could not be verified."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode('ascii'))


def encode_state(payload: StatePayload, *, key: bytes) -> str:
    body_bytes = json.dumps(asdict(payload), sort_keys=True,
                            separators=(',', ':')).encode('utf-8')
    body_b64 = _b64url_encode(body_bytes)
    sig = hmac.new(key, body_b64.encode('ascii'), sha256).digest()
    return body_b64 + '.' + _b64url_encode(sig)


def decode_state(token: str, *, key: bytes) -> StatePayload:
    if '.' not in token:
        raise StateInvalid("missing separator")
    body_b64, sig_b64 = token.split('.', 1)
    expected_sig = hmac.new(key, body_b64.encode('ascii'), sha256).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
    except Exception as e:
        raise StateInvalid("malformed signature") from e
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise StateInvalid("signature mismatch")
    try:
        body = json.loads(_b64url_decode(body_b64))
        payload = StatePayload(**body)
    except Exception as e:
        raise StateInvalid("malformed payload") from e
    if payload.exp < int(time.time()):
        raise StateExpired(f"state expired at {payload.exp}")
    return payload
```

- [ ] **Step 4: Run tests**

Expected: 6 passed.

- [ ] **Step 5: Commit**

```
git add src/localmail/api/admin/oauth_state.py tests/test_admin_oauth_state.py
git commit -m "feat(admin): HMAC-signed stateless OAuth state tokens"
```

---

## Task 8: Fake Google OAuth test double

**Files:**
- Create: `tests/_fake_google_oauth.py`

Mirror the spirit of `tests/_fake_imap.py`: in-memory representation of the
Google OAuth surface needed by the service layer. The real surface used is
`Flow.from_client_secrets_file().fetch_token(code=…)` returning credentials
with a `refresh_token` attribute, plus the consent-URL builder.

- [ ] **Step 1: Write the test double**

```python
# tests/_fake_google_oauth.py
"""In-memory stand-in for the Google OAuth Flow used by api/admin/oauth.py.

Not exposed via conftest — tests opt in by importing and monkeypatching.
"""

from dataclasses import dataclass


@dataclass
class FakeCredentials:
    refresh_token: str
    token: str = 'fake-access-token'


class FakeFlow:
    """Stand-in for google_auth_oauthlib.flow.Flow."""

    def __init__(self, *, redirect_uri='https://example.test/cb',
                 code_to_refresh: dict[str, str] | None = None):
        self.redirect_uri = redirect_uri
        self._code_to_refresh = code_to_refresh or {'good-code': 'refresh-xyz'}
        self.exchanged_codes: list[str] = []

    def authorization_url(self, *, state: str, prompt: str = 'consent',
                          access_type: str = 'offline') -> tuple[str, str]:
        return f'https://accounts.google.com/o/oauth2/auth?state={state}', state

    def fetch_token(self, *, code: str) -> dict:
        self.exchanged_codes.append(code)
        if code not in self._code_to_refresh:
            raise RuntimeError(f"unknown code {code!r}")
        return {'refresh_token': self._code_to_refresh[code]}

    @property
    def credentials(self) -> FakeCredentials:
        # The real Flow exposes credentials after fetch_token().
        last_code = self.exchanged_codes[-1]
        return FakeCredentials(refresh_token=self._code_to_refresh[last_code])
```

- [ ] **Step 2: Commit (no test yet — driven by Task 9)**

```
git add tests/_fake_google_oauth.py
git commit -m "test(admin): fake Google OAuth Flow double (mirrors FakeIMAPClient)"
```

---

## Task 9: OAuth service — `start_oauth` + `complete_oauth`

**Files:**
- Create: `src/localmail/api/admin/oauth.py`
- Test: `tests/test_admin_oauth.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_oauth.py
"""Service-layer tests for the admin web OAuth flow."""

import pytest
import keyring

from localmail.api.admin.oauth import (
    start_oauth, complete_oauth,
    PermissionDenied,
)
from localmail.api.admin.oauth_state import StateExpired, StateInvalid
from tests._fake_google_oauth import FakeFlow


KEY = b"k" * 32
CB = "https://example.test/admin/oauth/callback"


def _make_oauth_account(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "  oauth_provider, imap_host, imap_port, config) "
            "VALUES ('gm', 'g@example.test', 'oauth2', 'gmail', "
            "        'imap.gmail.com', 993, '{}'::jsonb) RETURNING id"
        )
        return cur.fetchone()[0]


@pytest.fixture
def fake_flow(monkeypatch):
    flow = FakeFlow()
    monkeypatch.setattr(
        'localmail.api.admin.oauth._build_flow',
        lambda *, redirect_uri: (flow.__setattr__('redirect_uri', redirect_uri) or flow),
    )
    return flow


def test_start_oauth_returns_consent_url_with_signed_state(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB)
    assert url.startswith('https://accounts.google.com/o/oauth2/auth?state=')


def test_complete_oauth_stores_refresh_token(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB)
    state = url.split('state=')[1]
    acct = complete_oauth(db_conn, state=state, code='good-code',
                          admin_user_id=42, signing_key=KEY,
                          redirect_uri=CB)
    assert acct.id == aid
    assert keyring.get_password('localmail', 'gm:refresh') == 'refresh-xyz'


def test_complete_oauth_rejects_cross_user_replay(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB)
    state = url.split('state=')[1]
    with pytest.raises(PermissionDenied):
        complete_oauth(db_conn, state=state, code='good-code',
                       admin_user_id=99, signing_key=KEY,
                       redirect_uri=CB)


def test_complete_oauth_rejects_tampered_state(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB)
    state = url.split('state=')[1]
    head, sig = state.split('.', 1)
    bad_state = head[:-1] + ('A' if head[-1] != 'A' else 'B') + '.' + sig
    with pytest.raises(StateInvalid):
        complete_oauth(db_conn, state=bad_state, code='good-code',
                       admin_user_id=42, signing_key=KEY,
                       redirect_uri=CB)
```

- [ ] **Step 2: Implement**

```python
# src/localmail/api/admin/oauth.py
"""Admin-UI web OAuth flow for Gmail (HMAC-signed stateless state).

Consumes [serve].state_signing_key (closes issue #114) and
[serve].oauth_callback_url. The CLI desktop loopback flow in
oauth_gmail.py stays in place — this module is purely additive.
"""

from __future__ import annotations

import secrets as _stdlib_secrets
import time

import psycopg

from localmail import secrets as _secrets
from localmail.api.admin.accounts import Account, get_account
from localmail.api.admin.oauth_state import (
    StatePayload, encode_state, decode_state,
)


_GOOGLE_SCOPES = ['https://mail.google.com/']
_NONCE_BYTES = 16
_STATE_TTL_SECONDS = 300


class PermissionDenied(RuntimeError):
    """Raised when the completing admin's user_id does not match the start."""


def _build_flow(*, redirect_uri: str):
    """Real Google OAuth Flow builder.

    Wrapped in a private helper so tests can monkeypatch.
    """
    from google_auth_oauthlib.flow import Flow  # type: ignore[import-not-found]
    flow = Flow.from_client_secrets_file(
        client_secrets_file=_secrets.gmail_client_secrets_path(),
        scopes=_GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


def start_oauth(conn: psycopg.Connection, account_id: int, *,
                admin_user_id: int,
                signing_key: bytes,
                redirect_uri: str) -> str:
    """Return a Google consent URL with a signed state token."""
    account = get_account(conn, account_id)
    if account.auth_method != 'oauth2' or account.oauth_provider != 'gmail':
        from localmail.api.admin.accounts import AccountFieldError
        raise AccountFieldError("start_oauth requires Gmail OAuth account")
    payload = StatePayload(
        user_id=admin_user_id,
        account_id=account_id,
        nonce=_stdlib_secrets.token_urlsafe(_NONCE_BYTES),
        exp=int(time.time()) + _STATE_TTL_SECONDS,
    )
    state = encode_state(payload, key=signing_key)
    flow = _build_flow(redirect_uri=redirect_uri)
    url, _state_echo = flow.authorization_url(
        state=state, prompt='consent', access_type='offline')
    return url


def complete_oauth(conn: psycopg.Connection, *,
                   state: str, code: str,
                   admin_user_id: int,
                   signing_key: bytes,
                   redirect_uri: str) -> Account:
    """Verify the state, exchange code, store refresh token, return account."""
    payload = decode_state(state, key=signing_key)
    if payload.user_id != admin_user_id:
        raise PermissionDenied(
            "OAuth state was minted for a different admin user"
        )
    account = get_account(conn, payload.account_id)
    flow = _build_flow(redirect_uri=redirect_uri)
    flow.fetch_token(code=code)
    refresh_token = flow.credentials.refresh_token
    _secrets.set_refresh_token(account.name, refresh_token)
    return account
```

(If `localmail.secrets` doesn't expose `set_refresh_token` / `gmail_client_secrets_path`,
use whatever wrapper functions are there — the tests pin the behavior.
The point is: don't touch keyring directly from this module.)

- [ ] **Step 3: Run tests**

Expected: 4 passed.

- [ ] **Step 4: Commit**

```
git add src/localmail/api/admin/oauth.py tests/test_admin_oauth.py
git commit -m "feat(admin): OAuth service — start/complete with HMAC state (#114)"
```

---

## Task 10: HTTP routes — accounts CRUD + secret + test-connection

**Files:**
- Create: `src/localmail/serve/admin/accounts_router.py`
- Test: `tests/test_serve_admin_accounts.py`

Endpoints from § 2A "HTTP shape":
```
GET    /v1/admin/accounts
POST   /v1/admin/accounts
GET    /v1/admin/accounts/{id}
PATCH  /v1/admin/accounts/{id}
DELETE /v1/admin/accounts/{id}?force=true|false
POST   /v1/admin/accounts/{id}/password
POST   /v1/admin/accounts/{id}/test-connection
```

All require admin session + CSRF (except GET routes, which don't need
CSRF). Re-use `localmail.serve.admin.dependencies.require_admin_session`
and `localmail.api.admin.csrf` per the existing pattern in
`auth_router.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_serve_admin_accounts.py
"""HTTP-route tests for /v1/admin/accounts (Sub-plan 2A)."""

import pytest
from fastapi.testclient import TestClient

# Re-use the admin-session fixture from the existing serve-admin tests
# (whatever it's called — likely `admin_client` or `logged_in_admin`).
# Look at tests/test_serve_admin_*.py to confirm the fixture names.

def test_list_accounts_returns_empty_when_none(admin_client):
    r = admin_client.get('/v1/admin/accounts')
    assert r.status_code == 200
    assert r.json() == {'accounts': []}


def test_create_account_password_round_trip(admin_client):
    body = {
        'name': 'created-via-api',
        'email_address': 'x@y.test',
        'auth_method': 'password',
        'imap_host': 'imap.example',
        'imap_port': 993,
    }
    r = admin_client.post('/v1/admin/accounts', json=body)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j['name'] == 'created-via-api'
    assert j['auth_method'] == 'password'

    r2 = admin_client.get(f"/v1/admin/accounts/{j['id']}")
    assert r2.status_code == 200
    assert r2.json()['id'] == j['id']


def test_create_account_validation_error_is_400(admin_client):
    r = admin_client.post('/v1/admin/accounts',
                          json={'name': '', 'auth_method': 'password',
                                'imap_host': 'h', 'imap_port': 993})
    assert r.status_code == 400


def test_patch_account_changes_folder_deny(admin_client):
    create = admin_client.post('/v1/admin/accounts', json={
        'name': 'patchable', 'email_address': 'a@b.test',
        'auth_method': 'password', 'imap_host': 'h', 'imap_port': 993,
    })
    aid = create.json()['id']
    r = admin_client.patch(
        f'/v1/admin/accounts/{aid}',
        json={'folder_deny': ['Spam', 'Trash']})
    assert r.status_code == 200
    assert r.json()['folder_deny'] == ['Spam', 'Trash']


def test_delete_empty_account_returns_204(admin_client):
    create = admin_client.post('/v1/admin/accounts', json={
        'name': 'deletable', 'email_address': 'x@y.test',
        'auth_method': 'password', 'imap_host': 'h', 'imap_port': 993,
    })
    aid = create.json()['id']
    r = admin_client.delete(f'/v1/admin/accounts/{aid}')
    assert r.status_code == 204


def test_post_password_stores_in_keyring(admin_client):
    import keyring
    create = admin_client.post('/v1/admin/accounts', json={
        'name': 'pw-target', 'email_address': 'x@y.test',
        'auth_method': 'password', 'imap_host': 'h', 'imap_port': 993,
    })
    aid = create.json()['id']
    r = admin_client.post(
        f'/v1/admin/accounts/{aid}/password',
        json={'password': 'sekret'})
    assert r.status_code == 204
    assert keyring.get_password('localmail', 'pw-target') == 'sekret'


def test_unauthenticated_request_redirects(client):
    r = client.get('/v1/admin/accounts', follow_redirects=False)
    assert r.status_code in (303, 401)
```

(Use the same `admin_client` / `client` fixture pattern from the existing
`tests/test_serve_admin_*.py` files. If a logged-in admin fixture
doesn't exist yet, write a small one in a new `conftest.py`-style helper
that POSTs `/admin/login` and re-uses the cookie.)

- [ ] **Step 2: Implement**

```python
# src/localmail/serve/admin/accounts_router.py
"""HTTP routes for /v1/admin/accounts (Sub-plan 2A).

Thin wrapper over localmail.api.admin.accounts. Every mutating route
requires admin session + CSRF.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from localmail.api.admin import accounts as svc
from localmail.api.admin.csrf import verify_csrf
from localmail.api.errors import NotFound
from localmail.serve.admin.dependencies import require_admin_session, AdminContext
from localmail.serve.deps import get_db_connection


router = APIRouter(tags=["admin-accounts"])


class _AccountIn(BaseModel):
    name: str
    email_address: str | None = None
    auth_method: Literal['password', 'oauth2', 'archive']
    imap_host: str | None = None
    imap_port: int | None = None
    oauth_provider: Literal['gmail'] | None = None
    folder_allow: list[str] | None = None
    folder_deny: list[str] | None = None
    folder_deny_flags: list[str] | None = None


class _AccountPatch(BaseModel):
    email_address: str | None = None
    auth_method: Literal['password', 'oauth2', 'archive'] | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    oauth_provider: Literal['gmail'] | None = None
    folder_allow: list[str] | None = None
    folder_deny: list[str] | None = None
    folder_deny_flags: list[str] | None = None
    sync_enabled: bool | None = None


class _PasswordIn(BaseModel):
    password: str = Field(min_length=1)


def _to_summary_dict(s: svc.AccountSummary) -> dict:
    return {'id': str(s.id), 'name': s.name,
            'email_address': s.email_address,
            'auth_method': s.auth_method,
            'sync_enabled': s.sync_enabled}


def _to_account_dict(a: svc.Account) -> dict:
    return {
        'id': str(a.id), 'name': a.name,
        'email_address': a.email_address,
        'auth_method': a.auth_method,
        'oauth_provider': a.oauth_provider,
        'imap_host': a.imap_host, 'imap_port': a.imap_port,
        'folder_allow': a.folder_allow,
        'folder_deny': a.folder_deny,
        'folder_deny_flags': a.folder_deny_flags,
        'sync_enabled': a.sync_enabled,
        'created_at': a.created_at.isoformat(),
        'updated_at': a.updated_at.isoformat(),
    }


@router.get("/accounts")
def list_accounts(ctx: AdminContext = Depends(require_admin_session),
                  conn=Depends(get_db_connection)):
    rows = svc.list_accounts(conn)
    return {'accounts': [_to_summary_dict(r) for r in rows]}


@router.post("/accounts", status_code=201)
def create_account(body: _AccountIn,
                   ctx: AdminContext = Depends(require_admin_session),
                   conn=Depends(get_db_connection),
                   _csrf=Depends(verify_csrf)):
    try:
        acct = svc.create_account(
            conn,
            name=body.name, email_address=body.email_address,
            auth_method=body.auth_method,
            imap_host=body.imap_host, imap_port=body.imap_port,
            oauth_provider=body.oauth_provider,
            folder_allow=body.folder_allow,
            folder_deny=body.folder_deny,
            folder_deny_flags=body.folder_deny_flags,
        )
    except svc.AccountFieldError as e:
        raise HTTPException(400, str(e))
    return _to_account_dict(acct)


@router.get("/accounts/{account_id}")
def get_account(account_id: str,
                ctx: AdminContext = Depends(require_admin_session),
                conn=Depends(get_db_connection)):
    try:
        return _to_account_dict(svc.get_account(conn, int(account_id)))
    except NotFound:
        raise HTTPException(404, "account not found")


@router.patch("/accounts/{account_id}")
def patch_account(account_id: str, body: _AccountPatch,
                  ctx: AdminContext = Depends(require_admin_session),
                  conn=Depends(get_db_connection),
                  _csrf=Depends(verify_csrf)):
    fields = body.model_dump(exclude_unset=True)
    try:
        acct = svc.update_account(conn, int(account_id), **fields)
    except NotFound:
        raise HTTPException(404, "account not found")
    except svc.AccountFieldError as e:
        raise HTTPException(400, str(e))
    return _to_account_dict(acct)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: str,
                   force: bool = Query(False),
                   ctx: AdminContext = Depends(require_admin_session),
                   conn=Depends(get_db_connection),
                   _csrf=Depends(verify_csrf)):
    try:
        svc.delete_account(conn, int(account_id), force=force)
    except NotFound:
        raise HTTPException(404, "account not found")
    except svc.AccountInUse as e:
        raise HTTPException(409, str(e))
    return Response(status_code=204)


@router.post("/accounts/{account_id}/password", status_code=204)
def post_password(account_id: str, body: _PasswordIn,
                  ctx: AdminContext = Depends(require_admin_session),
                  conn=Depends(get_db_connection),
                  _csrf=Depends(verify_csrf)):
    try:
        account = svc.get_account(conn, int(account_id))
    except NotFound:
        raise HTTPException(404, "account not found")
    try:
        svc.store_password(account, body.password)
    except svc.AccountFieldError as e:
        raise HTTPException(400, str(e))
    return Response(status_code=204)


@router.post("/accounts/{account_id}/test-connection")
def test_connection(account_id: str,
                    ctx: AdminContext = Depends(require_admin_session),
                    conn=Depends(get_db_connection),
                    _csrf=Depends(verify_csrf)):
    try:
        folders = svc.test_connection(conn, int(account_id))
    except NotFound:
        raise HTTPException(404, "account not found")
    except svc.AccountFieldError as e:
        raise HTTPException(400, str(e))
    return {'folders': [{'name': f.name, 'flags': list(f.flags)} for f in folders]}
```

(`AdminContext`, `get_db_connection`, and `verify_csrf` are the names
used by the existing auth_router / dashboard_router pattern — verify
the actual names during implementation and align. If the names differ,
update the imports here to match.)

- [ ] **Step 3: Register the router**

In `src/localmail/serve/app.py`, alongside the existing
`/v1/accounts` line, add `/v1/admin` for the new router:

```python
from localmail.serve.admin import accounts_router as admin_accounts_router

# … inside the registration block …
app.include_router(admin_accounts_router.router, prefix="/v1/admin")
```

- [ ] **Step 4: Run tests**

Expected: 7 passed.

- [ ] **Step 5: Commit**

```
git add src/localmail/serve/admin/accounts_router.py tests/test_serve_admin_accounts.py src/localmail/serve/app.py
git commit -m "feat(admin): /v1/admin/accounts HTTP routes — CRUD + password + test-connection"
```

---

## Task 11: HTTP routes — OAuth start + callback

**Files:**
- Create: `src/localmail/serve/admin/oauth_router.py`
- Test: `tests/test_serve_admin_oauth.py`

Endpoints:
```
POST /v1/admin/accounts/{id}/oauth/start    → {auth_url}
GET  /admin/oauth/callback?state=…&code=…   → 302 to /admin/accounts/{id}?oauth=success|failed
```

The start endpoint lives under `/v1/admin` (machine + UI). The callback
lives under `/admin` because it has to be redirected to by Google with
no Authorization header — it carries the cookie and goes HTML/redirect.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_serve_admin_oauth.py
"""HTTP-route tests for the admin OAuth start + callback (Sub-plan 2A)."""

import pytest
from tests._fake_google_oauth import FakeFlow


@pytest.fixture
def fake_flow(monkeypatch):
    flow = FakeFlow()
    monkeypatch.setattr(
        'localmail.api.admin.oauth._build_flow',
        lambda *, redirect_uri: (flow.__setattr__('redirect_uri', redirect_uri) or flow),
    )
    return flow


def _create_gmail_account(admin_client):
    r = admin_client.post('/v1/admin/accounts', json={
        'name': 'gm-http', 'email_address': 'g@example.test',
        'auth_method': 'oauth2', 'oauth_provider': 'gmail',
        'imap_host': 'imap.gmail.com', 'imap_port': 993,
    })
    assert r.status_code == 201
    return r.json()['id']


def test_oauth_start_returns_consent_url(admin_client, fake_flow):
    aid = _create_gmail_account(admin_client)
    r = admin_client.post(f'/v1/admin/accounts/{aid}/oauth/start')
    assert r.status_code == 200
    assert 'accounts.google.com' in r.json()['auth_url']


def test_oauth_callback_round_trip_stores_refresh(admin_client, fake_flow):
    import keyring
    aid = _create_gmail_account(admin_client)
    r1 = admin_client.post(f'/v1/admin/accounts/{aid}/oauth/start')
    state = r1.json()['auth_url'].split('state=')[1]
    r2 = admin_client.get(
        f'/admin/oauth/callback?state={state}&code=good-code',
        follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers['location'].endswith(f'/admin/accounts/{aid}?oauth=success')
    assert keyring.get_password('localmail', 'gm-http:refresh') == 'refresh-xyz'


def test_oauth_callback_failure_redirects_with_failed_flag(admin_client, fake_flow):
    aid = _create_gmail_account(admin_client)
    r1 = admin_client.post(f'/v1/admin/accounts/{aid}/oauth/start')
    state = r1.json()['auth_url'].split('state=')[1]
    r2 = admin_client.get(
        f'/admin/oauth/callback?state={state}&code=bad-code',
        follow_redirects=False)
    assert r2.status_code == 303
    assert 'oauth=failed' in r2.headers['location']
```

- [ ] **Step 2: Implement**

```python
# src/localmail/serve/admin/oauth_router.py
"""HTTP routes for the admin web OAuth flow (Sub-plan 2A)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from localmail.api.admin import oauth as svc
from localmail.api.admin.csrf import verify_csrf
from localmail.api.admin.oauth_state import StateExpired, StateInvalid
from localmail.api.errors import NotFound
from localmail.serve.admin.dependencies import require_admin_session, AdminContext
from localmail.serve.deps import get_config, get_db_connection


_HTTP_SEE_OTHER = 303


router = APIRouter(tags=["admin-oauth"])


@router.post("/accounts/{account_id}/oauth/start")
def start_oauth(account_id: str,
                ctx: AdminContext = Depends(require_admin_session),
                conn=Depends(get_db_connection),
                cfg=Depends(get_config),
                _csrf=Depends(verify_csrf)):
    try:
        url = svc.start_oauth(
            conn, int(account_id),
            admin_user_id=ctx.user_id,
            signing_key=cfg.serve.state_signing_key.encode('utf-8'),
            redirect_uri=cfg.serve.oauth_callback_url,
        )
    except NotFound:
        raise HTTPException(404, "account not found")
    return {'auth_url': url}


@router.get("/oauth/callback")
def oauth_callback(state: str = Query(...),
                   code: str = Query(...),
                   ctx: AdminContext = Depends(require_admin_session),
                   conn=Depends(get_db_connection),
                   cfg=Depends(get_config)):
    try:
        account = svc.complete_oauth(
            conn,
            state=state, code=code,
            admin_user_id=ctx.user_id,
            signing_key=cfg.serve.state_signing_key.encode('utf-8'),
            redirect_uri=cfg.serve.oauth_callback_url,
        )
    except (StateInvalid, StateExpired, svc.PermissionDenied):
        # Surface the failure without leaking which check failed —
        # the redirect carries oauth=failed and the operator-side
        # log records the exact reason.
        return RedirectResponse('/admin?oauth=failed',
                                status_code=_HTTP_SEE_OTHER)
    except Exception:
        return RedirectResponse('/admin?oauth=failed',
                                status_code=_HTTP_SEE_OTHER)
    return RedirectResponse(
        f'/admin/accounts/{account.id}?oauth=success',
        status_code=_HTTP_SEE_OTHER,
    )
```

- [ ] **Step 3: Register the router**

In `src/localmail/serve/app.py`:

```python
from localmail.serve.admin import oauth_router as admin_oauth_router

# Mount the start endpoint at /v1/admin (machine + UI):
app.include_router(admin_oauth_router.router, prefix="/v1/admin",
                   include_in_schema=True)
# Mount the callback at /admin (Google redirects here, browser only):
# The router exposes both routes; FastAPI will register the callback
# under /admin/oauth/callback when included with prefix="/admin"
# AND under /v1/admin/oauth/callback when included with /v1/admin.
# To avoid the duplicate, split the router into two pieces:
```

Actually — simpler: split into two routers in the same module (`router_v1`
for the start endpoint, `router_admin` for the callback) and mount each
with its own prefix:

```python
# in oauth_router.py
router_v1 = APIRouter(tags=["admin-oauth-api"])
router_admin = APIRouter(tags=["admin-oauth-callback"])

@router_v1.post("/accounts/{account_id}/oauth/start") …
@router_admin.get("/oauth/callback") …
```

```python
# in serve/app.py
app.include_router(admin_oauth_router.router_v1, prefix="/v1/admin")
app.include_router(admin_oauth_router.router_admin, prefix="/admin")
```

- [ ] **Step 4: Run tests**

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add src/localmail/serve/admin/oauth_router.py tests/test_serve_admin_oauth.py src/localmail/serve/app.py
git commit -m "feat(admin): /v1/admin/.../oauth/start + /admin/oauth/callback routes"
```

---

## Task 12: CLAUDE.md update + full-suite verification

**Files:**
- Modify: `CLAUDE.md`

Add a one-paragraph note documenting Sub-plan 2A as the cross-cutting
runtime invariant. Specifically:

- The new HMAC-state OAuth flow lives under
  `localmail.api.admin.oauth` and consumes `[serve].state_signing_key`
  (closes #114). The CLI desktop loopback flow stays in place;
  they coexist.
- DB-canonical accounts: migration 0020 adds `folder_allow`,
  `folder_deny`, `folder_deny_flags`, `sync_enabled`,
  `updated_at`, and the `accounts_live_requires_host` check
  constraint. v1 daemon still does not honor `sync_enabled` —
  that's a v1.x change.
- CLI rewiring (add-account, oauth-login, remove-account writing
  to DB) is deliberately deferred to Sub-plan 2A.2.

- [ ] **Step 1: Append the note** in the "GUI server (Phase 1 of GUI)" /
  "Admin session revocation" cluster of CLAUDE.md (the same section that
  documents PR #117). Use the same prose style as the
  "Admin session revocation (#113)" note — single paragraph, references
  to specific module paths, no bullets unless mirroring an existing list.

- [ ] **Step 2: Run the full suite**

```
unset VIRTUAL_ENV && uv run pytest -q tests/
```

Expected: every prior test still passes, plus the new ones from this PR.
Approximate target: **909 + ~30 new = ~939 passed**. If anything else
broke, debug before committing — don't change the count expectation
in the handoff.

- [ ] **Step 3: Mypy clean on touched files**

```
unset VIRTUAL_ENV && uv run mypy src/localmail/api/admin/accounts.py \
    src/localmail/api/admin/oauth.py \
    src/localmail/api/admin/oauth_state.py \
    src/localmail/serve/admin/accounts_router.py \
    src/localmail/serve/admin/oauth_router.py
```

Expected: 0 errors.

- [ ] **Step 4: Commit + open PR**

```
git add CLAUDE.md
git commit -m "docs(admin): document Sub-plan 2A — DB-canonical accounts + web OAuth"
```

Open PR with title `feat(admin): Sub-plan 2A — account management (closes #114)`
and body summarizing the four schema changes + the two new service
modules + the two new routers + the closing-#114 consumer wiring.

---

## Self-review checklist

Before opening the PR, walk through this:

**Spec coverage**:
- [ ] Migration 0020 — Task 1 ✓
- [ ] Service: list/get/create/update/delete/store_password/clear_secret/test_connection — Tasks 2-6 ✓
- [ ] OAuth flow with HMAC state — Tasks 7-9 ✓
- [ ] Closes #114 — `state_signing_key` consumed by `oauth.start_oauth` and `oauth.complete_oauth` via the new HTTP routes — Tasks 9 + 11 ✓
- [ ] HTTP shape — 9 endpoints, all in Tasks 10 + 11 ✓
- [ ] CLAUDE.md update — Task 12 ✓
- [ ] Tests for service + HTTP + HMAC roundtrip — Tasks 2-11 ✓
- [ ] Fake Google OAuth — Task 8 ✓
- [ ] CLI rewiring of existing commands — **deferred to 2A.2** (documented in pre-amble) ✓
- [ ] TOML→DB seed at init-db — **deferred to 2A.2** (documented) ✓
- [ ] Jinja2 templates — **deferred to 2A.3** (documented) ✓

**Placeholder scan**: none in this plan. Every step has the actual content.

**Type consistency**:
- `Account` and `AccountSummary` dataclasses are introduced once (Task 2)
  and re-used in every later task.
- `AccountFieldError`, `AccountInUse`, `NotFound`, `PermissionDenied`,
  `StateExpired`, `StateInvalid` — each defined exactly once, imported
  consistently.
- HTTP routes use `str` IDs on the wire per the project convention
  (CLAUDE.md "ID typing (#33)"). Service layer takes `int`. The cast
  happens at the route boundary via `int(account_id)`.
- `FolderInfo` defined in Task 6, re-used in Task 10.
- `StatePayload` defined in Task 7, used in Task 9.

**Open risks the implementer should know about**:
1. `localmail.serve.deps.get_config` and `get_db_connection` are the
   assumed dependency-injection helper names — verify they exist in
   the serve package; if they're named differently, align all
   `Depends(...)` calls accordingly.
2. The existing `tests/_fake_imap.py::FakeIMAPClient.with_folders`
   constructor probably doesn't exist yet — Task 6 mentions adding it
   if needed (5-line helper).
3. `verify_csrf` is the assumed name for the CSRF FastAPI dependency.
   The existing `localmail.api.admin.csrf` module exposes
   `generate_csrf_token` and a verifier; align the import.
4. `localmail.secrets` is assumed to expose `set_password`,
   `delete_password`, `set_refresh_token`, `delete_refresh_token`,
   and `gmail_client_secrets_path` — confirm names against the
   actual module before writing the service implementations.
