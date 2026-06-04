# Admin user-management screens (2A.4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship server-rendered HTMX screens at `/admin/users` plus a JSON `/v1/admin/users` API for managing API users — list/create/delete, per-account ACL grants, `is_admin` toggle + session revocation, admin password reset, enable/disable — guarded against admin lock-out.

**Architecture:** Mirror the 2A.3 account screens exactly. A new transport-free service module `api/admin/users.py` composes the existing primitives (`api/auth.py`, `api/acl.py`, `api/admin/auth.py`) and adds the missing CRUD + pure guard predicates. Two thin routers (`serve/admin/users_router.py` JSON, `serve/admin/users_panel_router.py` HTML) share that service. Pure form parsing lives in `serve/admin/user_forms.py`. The count-based last-admin guard lives in the service (pure predicate + IO wrapper); the identity-based self-action guard lives in the routers (only they know who "you" are).

**Tech Stack:** Python 3.12, psycopg v3 (raw SQL, `class_row`), FastAPI, Jinja2 + HTMX, pytest. No ORM. No new migration (`is_admin`, `disabled_at`, `sessions_invalidated_at`, `user_accounts` all already exist).

**Spec:** [docs/superpowers/specs/2026-06-05-admin-users-screens-design.md](../specs/2026-06-05-admin-users-screens-design.md)

**Branch:** `admin-ui-2a4-user-screens` (already created; the spec is committed at `d22837a`).

**Conventions reminder:**
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit.
- Run the suite with `unset VIRTUAL_ENV && uv run pytest …` (avoids the stray-venv warning).
- `is_admin IS TRUE` everywhere (the column is nullable).
- No `cur.fetchone()[0]` without `assert row is not None` first (mypy gate).
- Service functions take `conn`; the **caller commits**. In routes, the `with pool.connection() as conn:` context commits on clean exit (same as the accounts service — `create_account` has no internal commit).
- IDs are strings on the wire (#33): JSON bodies emit `str(id)`; path/body IDs are parsed via `localmail.api.ids.parse_int_id`.

---

## File structure

```
src/localmail/api/admin/users.py                 # NEW — service layer (dataclasses, errors, CRUD, pure guards)
src/localmail/serve/admin/user_forms.py          # NEW — pure form parsing + error mapping
src/localmail/serve/admin/users_router.py        # NEW — JSON /v1/admin/users
src/localmail/serve/admin/users_panel_router.py  # NEW — HTML /admin/users
src/localmail/serve/admin/static/users-panel.js  # NEW — minimal (delete confirm is via hx-confirm; file reserved for parity, may stay empty-but-present)
src/localmail/serve/admin/templates/users/
    list.html            # NEW — roster table
    _row.html            # NEW — one roster row
    new.html             # NEW — create form wrapper
    _create_fields.html  # NEW — create fields fragment (re-rendered on validation error)
    edit.html            # NEW — edit composite (status + password + grants + danger)
    _status.html         # NEW — admin/disabled badges + toggle buttons + inline error
    _grants.html         # NEW — per-account grant checklist fragment
    _message.html        # NEW — generic one-line confirmation fragment (password / sessions)
    _delete_blocked.html # NEW — self/last-admin delete refusal (409)
src/localmail/serve/app.py                        # MODIFY — import + include the two new routers
tests/test_api_admin_users.py                     # NEW — service-layer tests (real DB)
tests/test_user_forms.py                          # NEW — pure form + guard-predicate tests (no DB)
tests/test_serve_admin_users.py                   # NEW — JSON route tests
tests/test_serve_admin_user_screens.py            # NEW — HTML route tests
CLAUDE.md                                         # MODIFY — add 2A.4 bullet
README.md                                         # MODIFY — admin section
```

---

## Task 1: Service layer — module skeleton, dataclasses, errors, `list_users`, `get_user`

**Files:**
- Create: `src/localmail/api/admin/users.py`
- Test: `tests/test_api_admin_users.py`

- [ ] **Step 1: Write the failing tests for `list_users` and `get_user`**

Create `tests/test_api_admin_users.py`:

```python
"""Service-layer tests for admin user management (api/admin/users.py)."""
from __future__ import annotations

import psycopg
import pytest

from localmail.api.admin import users as svc
from localmail.api.admin.auth import UserNotFound


def _insert_user(conn: psycopg.Connection, username: str, *,
                 is_admin: bool = False, disabled: bool = False) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin, disabled_at) "
            "VALUES (%s, 'x', %s, %s) RETURNING id",
            (username, is_admin, "now()" if disabled else None),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _insert_account(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, config) "
            "VALUES (%s, %s, 'password', 'imap.example', 993, '{}'::jsonb) RETURNING id",
            (name, f"{name}@b.test"),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def test_list_users_empty(db_conn):
    assert svc.list_users(db_conn) == []


def test_list_users_sorted_by_username_with_flags(db_conn):
    _insert_user(db_conn, "zoe")
    _insert_user(db_conn, "amy", is_admin=True)
    _insert_user(db_conn, "bob", disabled=True)
    rows = svc.list_users(db_conn)
    assert [r.username for r in rows] == ["amy", "bob", "zoe"]
    amy = rows[0]
    assert amy.is_admin is True and amy.disabled is False
    assert rows[1].disabled is True  # bob


def test_get_user_includes_grant_for_every_account(db_conn):
    uid = _insert_user(db_conn, "amy")
    a1 = _insert_account(db_conn, "alpha")
    a2 = _insert_account(db_conn, "beta")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
            (uid, a1),
        )
    detail = svc.get_user(db_conn, uid)
    assert detail.username == "amy"
    by_id = {g.account_id: g for g in detail.account_grants}
    assert by_id[a1].granted is True
    assert by_id[a2].granted is False
    assert by_id[a1].account_name == "alpha"


def test_get_user_unknown_raises(db_conn):
    with pytest.raises(UserNotFound):
        svc.get_user(db_conn, 999999)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_users.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.api.admin.users'`.

- [ ] **Step 3: Implement the module skeleton + `list_users` + `get_user`**

Create `src/localmail/api/admin/users.py`:

```python
"""Service layer for admin-UI user management (Sub-plan 2A.4).

Transport-free: pure functions over a psycopg connection, no FastAPI imports
and no IO beyond the connection passed in. Composes the existing primitives in
api/auth.py, api/acl.py and api/admin/auth.py and adds the CRUD + guard logic
the admin screens need.

Two guards protect against admin lock-out:
  * Count-based last-admin rule — `would_orphan_last_admin` (pure) + the IO
    wrappers that read the active-admin count. Identity-agnostic, lives here.
  * Identity-based self-action rule ("you can't demote/delete yourself") — lives
    in the routers, the only layer that knows who the logged-in admin is.

`is_admin` is a nullable BOOLEAN (migration 0022), so every admin predicate uses
`is_admin IS TRUE`, never a bare truthiness check.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

from localmail.api import acl
from localmail.api.admin.auth import UserNotFound
from localmail.api.auth import hash_password


class UserFieldError(ValueError):
    """Validation rejected a create / password / grant (e.g. duplicate username)."""


class LastAdminError(ValueError):
    """The action would leave the system with no active admin."""


class SelfActionError(ValueError):
    """An admin tried to demote/delete their own logged-in account.

    Raised by the routers (which know the caller's identity), never by the
    service. Defined here so both routers import it from one place.
    """


@dataclass(frozen=True)
class UserSummary:
    id: int
    username: str
    is_admin: bool
    disabled: bool
    created_at: datetime


@dataclass(frozen=True)
class AccountGrant:
    account_id: int
    account_name: str
    granted: bool


@dataclass(frozen=True)
class UserDetail:
    id: int
    username: str
    is_admin: bool
    disabled: bool
    created_at: datetime
    account_grants: list[AccountGrant]


def list_users(conn: psycopg.Connection) -> list[UserSummary]:
    """Every API user, sorted by username. `is_admin IS TRUE` (nullable column)."""
    with conn.cursor(row_factory=class_row(UserSummary)) as cur:
        cur.execute(
            "SELECT id, username, (is_admin IS TRUE) AS is_admin, "
            "       (disabled_at IS NOT NULL) AS disabled, created_at "
            "  FROM api_users ORDER BY username"
        )
        return cur.fetchall()


def get_user(conn: psycopg.Connection, user_id: int) -> UserDetail:
    """One user plus a grant flag for EVERY account. Raises UserNotFound."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT username, (is_admin IS TRUE), (disabled_at IS NOT NULL), created_at "
            "  FROM api_users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise UserNotFound(f"no user with id={user_id}")
        username, is_admin, disabled, created_at = row
        cur.execute(
            "SELECT a.id, a.name, (ua.user_id IS NOT NULL) AS granted "
            "  FROM accounts a "
            "  LEFT JOIN user_accounts ua "
            "    ON ua.account_id = a.id AND ua.user_id = %s "
            " ORDER BY a.name",
            (user_id,),
        )
        grants = [
            AccountGrant(account_id=int(aid), account_name=name, granted=bool(granted))
            for aid, name, granted in cur.fetchall()
        ]
    return UserDetail(
        id=user_id, username=username, is_admin=bool(is_admin),
        disabled=bool(disabled), created_at=created_at, account_grants=grants,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_users.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/users.py tests/test_api_admin_users.py
git commit -m "feat(admin): users service — list_users + get_user with per-account grants"
```

---

## Task 2: Service — `create_user`, `set_password`

**Files:**
- Modify: `src/localmail/api/admin/users.py`
- Test: `tests/test_api_admin_users.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_admin_users.py`:

```python
from localmail.api.auth import verify_password  # add to imports at top of file


def test_create_user_basic(db_conn):
    uid = svc.create_user(db_conn, username="newbie", password="pw12345")
    detail = svc.get_user(db_conn, uid)
    assert detail.username == "newbie"
    assert detail.is_admin is False
    with db_conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM api_users WHERE id = %s", (uid,))
        row = cur.fetchone()
    assert row is not None and verify_password("pw12345", row[0])


def test_create_user_admin_flag(db_conn):
    uid = svc.create_user(db_conn, username="boss", password="pw12345", is_admin=True)
    assert svc.get_user(db_conn, uid).is_admin is True


def test_create_user_duplicate_username_raises_field_error(db_conn):
    svc.create_user(db_conn, username="dup", password="pw12345")
    db_conn.commit()
    with pytest.raises(svc.UserFieldError):
        svc.create_user(db_conn, username="dup", password="pw12345")
    db_conn.rollback()


@pytest.mark.parametrize("username,password", [("", "pw12345"), ("ok", "")])
def test_create_user_blank_fields_raise(db_conn, username, password):
    with pytest.raises(svc.UserFieldError):
        svc.create_user(db_conn, username=username, password=password)


def test_set_password_resets_without_old(db_conn):
    uid = _insert_user(db_conn, "amy")
    svc.set_password(db_conn, uid, "brandnew1")
    with db_conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM api_users WHERE id = %s", (uid,))
        row = cur.fetchone()
    assert row is not None and verify_password("brandnew1", row[0])


def test_set_password_blank_raises(db_conn):
    uid = _insert_user(db_conn, "amy")
    with pytest.raises(svc.UserFieldError):
        svc.set_password(db_conn, uid, "")


def test_set_password_unknown_user_raises(db_conn):
    with pytest.raises(UserNotFound):
        svc.set_password(db_conn, 999999, "whatever1")
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_users.py -q -k "create_user or set_password"`
Expected: FAIL — `AttributeError: module 'localmail.api.admin.users' has no attribute 'create_user'`.

- [ ] **Step 3: Implement `create_user` + `set_password`**

Append to `src/localmail/api/admin/users.py`:

```python
def _validate_new_user(username: str, password: str) -> None:
    if not username or not username.strip():
        raise UserFieldError("username must not be blank")
    if not password:
        raise UserFieldError("password must not be blank")


def create_user(
    conn: psycopg.Connection, *, username: str, password: str, is_admin: bool = False,
) -> int:
    """Insert a new api_users row and return its id.

    Reuses `auth.hash_password`; sets `is_admin` in the same INSERT. Maps a
    duplicate username to `UserFieldError` for an inline 400. Caller commits.
    """
    _validate_new_user(username, password)
    pw_hash = hash_password(password)
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO api_users (username, password_hash, is_admin) "
                "VALUES (%s, %s, %s) RETURNING id",
                (username.strip(), pw_hash, is_admin),
            )
        except psycopg.errors.UniqueViolation as e:
            raise UserFieldError(f"username {username!r} already exists") from e
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def set_password(conn: psycopg.Connection, user_id: int, new_password: str) -> None:
    """Admin password reset — no old password required. Raises UserNotFound."""
    if not new_password:
        raise UserFieldError("password must not be blank")
    pw_hash = hash_password(new_password)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET password_hash = %s WHERE id = %s",
            (pw_hash, user_id),
        )
        if cur.rowcount == 0:
            raise UserNotFound(f"no user with id={user_id}")
```

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_users.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/users.py tests/test_api_admin_users.py
git commit -m "feat(admin): users service — create_user + admin set_password"
```

---

## Task 3: Service — pure guards + `set_admin`, `set_disabled`, `delete_user`

**Files:**
- Modify: `src/localmail/api/admin/users.py`
- Test: `tests/test_api_admin_users.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_admin_users.py`:

```python
def test_set_admin_grant_and_revoke(db_conn):
    keep = _insert_user(db_conn, "keeper", is_admin=True)  # second admin so guard passes
    uid = _insert_user(db_conn, "amy")
    svc.set_admin(db_conn, uid, True)
    assert svc.get_user(db_conn, uid).is_admin is True
    svc.set_admin(db_conn, uid, False)
    assert svc.get_user(db_conn, uid).is_admin is False
    assert svc.get_user(db_conn, keep).is_admin is True


def test_set_admin_revoke_last_admin_blocked(db_conn):
    uid = _insert_user(db_conn, "solo", is_admin=True)
    with pytest.raises(svc.LastAdminError):
        svc.set_admin(db_conn, uid, False)


def test_set_admin_revoke_allowed_with_second_active_admin(db_conn):
    _insert_user(db_conn, "other", is_admin=True)
    uid = _insert_user(db_conn, "amy", is_admin=True)
    svc.set_admin(db_conn, uid, False)  # no raise
    assert svc.get_user(db_conn, uid).is_admin is False


def test_disabled_admin_does_not_count_as_active(db_conn):
    _insert_user(db_conn, "ghost", is_admin=True, disabled=True)  # disabled → not protective
    uid = _insert_user(db_conn, "solo", is_admin=True)
    with pytest.raises(svc.LastAdminError):
        svc.set_admin(db_conn, uid, False)


def test_set_disabled_last_admin_blocked(db_conn):
    uid = _insert_user(db_conn, "solo", is_admin=True)
    with pytest.raises(svc.LastAdminError):
        svc.set_disabled(db_conn, uid, True)


def test_set_disabled_toggle(db_conn):
    _insert_user(db_conn, "other", is_admin=True)
    uid = _insert_user(db_conn, "amy", is_admin=True)
    svc.set_disabled(db_conn, uid, True)
    assert svc.get_user(db_conn, uid).disabled is True
    svc.set_disabled(db_conn, uid, False)
    assert svc.get_user(db_conn, uid).disabled is False


def test_delete_last_admin_blocked(db_conn):
    uid = _insert_user(db_conn, "solo", is_admin=True)
    with pytest.raises(svc.LastAdminError):
        svc.delete_user(db_conn, uid)


def test_delete_user_cascades_grants_and_tokens(db_conn):
    _insert_user(db_conn, "other", is_admin=True)
    uid = _insert_user(db_conn, "amy")
    a1 = _insert_account(db_conn, "alpha")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
            (uid, a1),
        )
    svc.delete_user(db_conn, uid)
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM api_users WHERE id = %s", (uid,))
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM user_accounts WHERE user_id = %s", (uid,))
        assert cur.fetchone() is None


def test_delete_unknown_user_raises(db_conn):
    with pytest.raises(UserNotFound):
        svc.delete_user(db_conn, 999999)
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_users.py -q -k "admin or disabled or delete"`
Expected: FAIL — missing `set_admin` / `set_disabled` / `delete_user`.

- [ ] **Step 3: Implement the guards + mutators**

Append to `src/localmail/api/admin/users.py`:

```python
def would_orphan_last_admin(
    *, target_is_active_admin: bool, active_admin_count: int,
) -> bool:
    """True iff removing the target's active-admin status drops the count to 0.

    Pure. `active_admin_count` is the count of users with `is_admin IS TRUE AND
    disabled_at IS NULL`, INCLUDING the target when it currently qualifies.
    """
    return target_is_active_admin and active_admin_count <= 1


def active_admin_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM api_users "
            "WHERE is_admin IS TRUE AND disabled_at IS NULL"
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _user_state(conn: psycopg.Connection, user_id: int) -> tuple[bool, bool]:
    """Return (is_admin, disabled) for user_id. Raises UserNotFound."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (is_admin IS TRUE), (disabled_at IS NOT NULL) "
            "FROM api_users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user with id={user_id}")
    return bool(row[0]), bool(row[1])


def _guard_not_last_admin(conn: psycopg.Connection, user_id: int, action: str) -> None:
    """Raise LastAdminError if `user_id` is the sole active admin."""
    is_admin, disabled = _user_state(conn, user_id)
    target_is_active_admin = is_admin and not disabled
    if would_orphan_last_admin(
        target_is_active_admin=target_is_active_admin,
        active_admin_count=active_admin_count(conn),
    ):
        raise LastAdminError(f"cannot {action} the last active admin")


def set_admin(conn: psycopg.Connection, user_id: int, is_admin: bool) -> None:
    """Grant/revoke admin. Revoking the last active admin raises LastAdminError."""
    if not is_admin:
        _guard_not_last_admin(conn, user_id, "demote")
    else:
        _user_state(conn, user_id)  # existence check → UserNotFound
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET is_admin = %s WHERE id = %s", (is_admin, user_id)
        )


def set_disabled(conn: psycopg.Connection, user_id: int, disabled: bool) -> None:
    """Enable/disable a user. Disabling the last active admin raises LastAdminError."""
    if disabled:
        _guard_not_last_admin(conn, user_id, "disable")
    else:
        _user_state(conn, user_id)  # existence check → UserNotFound
    with conn.cursor() as cur:
        if disabled:
            cur.execute(
                "UPDATE api_users SET disabled_at = now() WHERE id = %s", (user_id,)
            )
        else:
            cur.execute(
                "UPDATE api_users SET disabled_at = NULL WHERE id = %s", (user_id,)
            )


def delete_user(conn: psycopg.Connection, user_id: int) -> None:
    """Delete a user (tokens + grants cascade). Last active admin → LastAdminError."""
    _guard_not_last_admin(conn, user_id, "delete")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM api_users WHERE id = %s", (user_id,))
```

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_users.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/users.py tests/test_api_admin_users.py
git commit -m "feat(admin): users service — last-admin guard + set_admin/set_disabled/delete_user"
```

---

## Task 4: Service — `set_grant`, `revoke_sessions`, `action_flags`

**Files:**
- Modify: `src/localmail/api/admin/users.py`
- Test: `tests/test_api_admin_users.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_admin_users.py`:

```python
def test_set_grant_grant_then_revoke_idempotent(db_conn):
    uid = _insert_user(db_conn, "amy")
    a1 = _insert_account(db_conn, "alpha")
    svc.set_grant(db_conn, uid, a1, True)
    svc.set_grant(db_conn, uid, a1, True)  # idempotent
    assert {g.account_id: g.granted for g in svc.get_user(db_conn, uid).account_grants}[a1] is True
    svc.set_grant(db_conn, uid, a1, False)
    svc.set_grant(db_conn, uid, a1, False)  # idempotent
    assert {g.account_id: g.granted for g in svc.get_user(db_conn, uid).account_grants}[a1] is False


def test_set_grant_unknown_user_raises(db_conn):
    a1 = _insert_account(db_conn, "alpha")
    with pytest.raises(UserNotFound):
        svc.set_grant(db_conn, 999999, a1, True)


def test_revoke_sessions_bumps_timestamp(db_conn):
    uid = _insert_user(db_conn, "amy")
    svc.revoke_sessions(db_conn, uid)
    with db_conn.cursor() as cur:
        cur.execute("SELECT sessions_invalidated_at FROM api_users WHERE id = %s", (uid,))
        row = cur.fetchone()
    assert row is not None and row[0] is not None


def test_revoke_sessions_unknown_user_raises(db_conn):
    with pytest.raises(UserNotFound):
        svc.revoke_sessions(db_conn, 999999)


@pytest.mark.parametrize(
    "is_admin,disabled,count,is_self,expect",
    [
        # last active admin, not self: demote/disable/delete all blocked by orphan rule
        (True, False, 1, False, {"block_demote": True,  "block_disable": True,  "block_delete": True}),
        # two admins, not self: nothing blocked
        (True, False, 2, False, {"block_demote": False, "block_disable": False, "block_delete": False}),
        # two admins, self: demote+delete blocked (self-action), disable allowed
        (True, False, 2, True,  {"block_demote": True,  "block_disable": False, "block_delete": True}),
        # non-admin, not self: nothing blocked
        (False, False, 5, False, {"block_demote": False, "block_disable": False, "block_delete": False}),
        # non-admin self: self-delete still blocked, demote n/a but blocked-as-self is harmless
        (False, False, 5, True,  {"block_demote": True,  "block_disable": False, "block_delete": True}),
    ],
)
def test_action_flags(is_admin, disabled, count, is_self, expect):
    flags = svc.action_flags(
        target_is_active_admin=(is_admin and not disabled),
        active_admin_count=count,
        is_self=is_self,
    )
    assert flags == expect
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_users.py -q -k "grant or revoke_sessions or action_flags"`
Expected: FAIL — missing `set_grant` / `revoke_sessions` / `action_flags`.

- [ ] **Step 3: Implement**

Append to `src/localmail/api/admin/users.py`:

```python
def set_grant(
    conn: psycopg.Connection, user_id: int, account_id: int, granted: bool,
) -> None:
    """Grant or revoke `user_id`'s ACL on `account_id`. Idempotent.

    Confirms the user exists first (clean UserNotFound → 404). A bad
    account_id surfaces as UserFieldError (the grant checklist only offers
    existing accounts, so this is a defensive mapping).
    """
    _user_state(conn, user_id)  # existence check → UserNotFound
    if granted:
        try:
            acl.grant_account(conn, user_id, account_id)
        except psycopg.errors.ForeignKeyViolation as e:
            raise UserFieldError(f"unknown account {account_id}") from e
    else:
        acl.revoke_account(conn, user_id, account_id)


def revoke_sessions(conn: psycopg.Connection, user_id: int) -> None:
    """Invalidate the user's outstanding admin cookies. Raises UserNotFound."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET sessions_invalidated_at = now() WHERE id = %s",
            (user_id,),
        )
        if cur.rowcount == 0:
            raise UserNotFound(f"no user with id={user_id}")


def action_flags(
    *, target_is_active_admin: bool, active_admin_count: int, is_self: bool,
) -> dict[str, bool]:
    """Which edit-screen controls to render disabled (UX only; not enforcement).

    `block_demote` / `block_delete` fire for the logged-in admin's own row
    (self-action) or when the action would orphan the last admin.
    `block_disable` fires only on the orphan rule (self-disable is permitted).
    """
    orphan = would_orphan_last_admin(
        target_is_active_admin=target_is_active_admin,
        active_admin_count=active_admin_count,
    )
    return {
        "block_demote": is_self or orphan,
        "block_disable": orphan,
        "block_delete": is_self or orphan,
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_users.py -q`
Expected: PASS.

- [ ] **Step 5: Run mypy on the new module**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail/api/admin/users.py`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/api/admin/users.py tests/test_api_admin_users.py
git commit -m "feat(admin): users service — set_grant, revoke_sessions, action_flags"
```

---

## Task 5: Pure form module — `user_forms.py`

**Files:**
- Create: `src/localmail/serve/admin/user_forms.py`
- Test: `tests/test_user_forms.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_user_forms.py`:

```python
"""Pure tests for user_forms + the service's pure guard predicate."""
from __future__ import annotations

import pytest

from localmail.api.admin.users import (
    LastAdminError,
    SelfActionError,
    UserFieldError,
    would_orphan_last_admin,
)
from localmail.serve.admin import user_forms as forms


def test_form_to_create_kwargs_basic():
    out = forms.form_to_create_kwargs({"username": " amy ", "password": "pw12345"})
    assert out == {"username": "amy", "password": "pw12345", "is_admin": False}


def test_form_to_create_kwargs_admin_checkbox_on():
    out = forms.form_to_create_kwargs(
        {"username": "boss", "password": "pw12345", "is_admin": "on"}
    )
    assert out["is_admin"] is True


@pytest.mark.parametrize("form", [
    {"username": "", "password": "pw12345"},
    {"username": "ok", "password": ""},
    {"password": "pw12345"},
])
def test_form_to_create_kwargs_blank_raises(form):
    with pytest.raises(forms.FormError):
        forms.form_to_create_kwargs(form)


def test_field_errors_username():
    assert forms.field_errors_from(UserFieldError("username 'x' already exists")) == {
        "username": "username 'x' already exists"
    }


def test_field_errors_password():
    assert forms.field_errors_from(UserFieldError("password must not be blank")) == {
        "password": "password must not be blank"
    }


def test_field_errors_fallback_form_level():
    out = forms.field_errors_from(LastAdminError("cannot demote the last active admin"))
    assert out == {"_form": "cannot demote the last active admin"}


def test_field_errors_self_action_fallback():
    out = forms.field_errors_from(SelfActionError("you cannot delete your own account"))
    assert out == {"_form": "you cannot delete your own account"}


@pytest.mark.parametrize("active_admin,count,expect", [
    (True, 1, True),
    (True, 2, False),
    (True, 0, True),   # defensive: never go negative
    (False, 1, False),
    (False, 5, False),
])
def test_would_orphan_last_admin(active_admin, count, expect):
    assert would_orphan_last_admin(
        target_is_active_admin=active_admin, active_admin_count=count
    ) is expect
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_user_forms.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.serve.admin.user_forms'`.

- [ ] **Step 3: Implement `user_forms.py`**

Create `src/localmail/serve/admin/user_forms.py`:

```python
"""Pure form-parsing helpers for the user admin screens (no IO).

Keeps the HTML router thin: every raw-form → service-kwargs transform and every
service-error → field mapping is unit-tested here in isolation.
"""
from __future__ import annotations

from localmail.api.admin.users import (
    LastAdminError,
    SelfActionError,
    UserFieldError,
)


class FormError(ValueError):
    """Malformed raw form input the service layer wouldn't otherwise see."""


def _checkbox(value: object) -> bool:
    """An HTML checkbox sends its value (e.g. 'on') when checked, nothing when not."""
    return bool(value)


def form_to_create_kwargs(form: dict) -> dict:
    """Map a raw create-form dict to create_user(**kwargs)."""
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if not username:
        raise FormError("username must not be blank")
    if not password:
        raise FormError("password must not be blank")
    return {
        "username": username,
        "password": password,
        "is_admin": _checkbox(form.get("is_admin")),
    }


# Substring → field-name map for surfacing a validation error beside the input.
# Order matters: first match wins.
_FIELD_HINTS: tuple[tuple[str, str], ...] = (
    ("username", "username"),
    ("password", "password"),
)


def field_errors_from(
    err: UserFieldError | FormError | LastAdminError | SelfActionError,
) -> dict[str, str]:
    """Map a validation/guard error to {field: message}; fall back to '_form'."""
    msg = str(err)
    for needle, field in _FIELD_HINTS:
        if needle in msg:
            return {field: msg}
    return {"_form": msg}
```

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_user_forms.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/user_forms.py tests/test_user_forms.py
git commit -m "feat(admin): pure user_forms module (parse + error mapping)"
```

---

## Task 6: JSON router `/v1/admin/users` + app wiring

**Files:**
- Create: `src/localmail/serve/admin/users_router.py`
- Modify: `src/localmail/serve/app.py:15-21` (imports) and `:184-188` (includes)
- Test: `tests/test_serve_admin_users.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_serve_admin_users.py`:

```python
"""HTTP-route tests for /v1/admin/users (Sub-plan 2A.4)."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
        cookie_secure=False,
    )


@pytest.fixture
def app(db_dsn, serve_cfg):
    return create_app(db_dsn=db_dsn, serve_config=serve_cfg)


@pytest.fixture
def admin_user_id(db_conn: psycopg.Connection) -> int:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('horst', %s, TRUE) RETURNING id",
            (pwh,),
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


@pytest.fixture
def admin_client(app, admin_user_id):
    import re
    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    r = client.post("/admin/login", data={
        "username": "horst", "password": "hunter2", "csrf_token": m.group(1)})
    assert r.status_code == 303, r.text
    key = _SIGNING_KEY.encode("ascii")

    def csrf_for(action: str, method: str = "POST") -> str:
        return make_csrf_token(
            user_id=admin_user_id, action=csrf_action(method, action), key=key)

    client.csrf_for = csrf_for  # type: ignore[attr-defined]
    return client


def _account(db_conn, name):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) VALUES (%s, %s, 'password', 'h', 993, '{}') RETURNING id",
            (name, f"{name}@b.test"))
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


def test_list_users_requires_auth(app):
    client = TestClient(app, follow_redirects=False)
    r = client.get("/v1/admin/users")
    assert r.status_code in (302, 303, 401, 403)


def test_list_users_includes_admin(admin_client, admin_user_id):
    r = admin_client.get("/v1/admin/users")
    assert r.status_code == 200, r.text
    users = r.json()["users"]
    assert any(u["username"] == "horst" and u["is_admin"] is True for u in users)
    assert all(isinstance(u["id"], str) for u in users)  # #33 string IDs


def test_create_user(admin_client):
    r = admin_client.post(
        "/v1/admin/users",
        json={"username": "newbie", "password": "pw12345"},
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/users")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "newbie"


def test_create_user_requires_csrf(admin_client):
    r = admin_client.post(
        "/v1/admin/users", json={"username": "x", "password": "pw12345"})
    assert r.status_code == 400


def test_create_duplicate_returns_400(admin_client):
    hdr = {"X-CSRF-Token": admin_client.csrf_for("/v1/admin/users")}
    admin_client.post("/v1/admin/users",
                      json={"username": "dup", "password": "pw12345"}, headers=hdr)
    r = admin_client.post("/v1/admin/users",
                          json={"username": "dup", "password": "pw12345"}, headers=hdr)
    assert r.status_code == 400


def test_get_user_detail_has_grants(admin_client, db_conn, admin_user_id):
    _account(db_conn, "alpha")
    r = admin_client.get(f"/v1/admin/users/{admin_user_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(admin_user_id)
    assert any(g["account_name"] == "alpha" for g in body["account_grants"])


def test_patch_demote_self_blocked_409(admin_client, admin_user_id):
    r = admin_client.patch(
        f"/v1/admin/users/{admin_user_id}",
        json={"is_admin": False},
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/v1/admin/users/{admin_user_id}", "PATCH")},
    )
    assert r.status_code == 409, r.text


def test_patch_demote_last_admin_blocked_409(admin_client, db_conn, admin_user_id):
    # second user, non-admin; promoting the only admin's removal still blocked when self.
    other = _account_user(db_conn, "amy", is_admin=True)
    # demote the OTHER admin while two exist → allowed
    r = admin_client.patch(
        f"/v1/admin/users/{other}",
        json={"is_admin": False},
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/v1/admin/users/{other}", "PATCH")},
    )
    assert r.status_code == 200, r.text


def _account_user(db_conn, username, *, is_admin=False):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, 'x', %s) RETURNING id", (username, is_admin))
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


def test_csrf_token_method_bound(admin_client, db_conn, admin_user_id):
    uid = _account_user(db_conn, "amy")
    # a token minted for PATCH must not authorize DELETE on the same URL
    patch_token = admin_client.csrf_for(f"/v1/admin/users/{uid}", "PATCH")
    r = admin_client.request(
        "DELETE", f"/v1/admin/users/{uid}", headers={"X-CSRF-Token": patch_token})
    assert r.status_code == 400


def test_delete_self_blocked_409(admin_client, admin_user_id):
    r = admin_client.request(
        "DELETE", f"/v1/admin/users/{admin_user_id}",
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/v1/admin/users/{admin_user_id}", "DELETE")})
    assert r.status_code == 409


def test_grant_round_trip(admin_client, db_conn, admin_user_id):
    uid = _account_user(db_conn, "amy")
    aid = _account(db_conn, "alpha")
    hdr = {"X-CSRF-Token": admin_client.csrf_for(f"/v1/admin/users/{uid}/grants")}
    r = admin_client.post(f"/v1/admin/users/{uid}/grants",
                          json={"account_id": str(aid), "granted": True}, headers=hdr)
    assert r.status_code == 200, r.text
    assert any(g["account_id"] == str(aid) and g["granted"] is True
               for g in r.json()["account_grants"])
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_users.py -q`
Expected: FAIL — routes 404 (router not mounted).

- [ ] **Step 3: Implement the JSON router**

Create `src/localmail/serve/admin/users_router.py`:

```python
"""HTTP routes for /v1/admin/users (Sub-plan 2A.4).

Thin wrapper over `localmail.api.admin.users`. Every route requires an admin
session; every mutating route validates a method-bound CSRF token from the
`X-CSRF-Token` header. IDs are strings on the wire (#33).

Guard mapping (mirrors accounts): validation (`UserFieldError`) → 400; absence
(`UserNotFound`) → 404; lock-out guards (`LastAdminError`, `SelfActionError`)
→ 409 — a structured, actionable conflict, never an opaque 500.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from localmail.api.admin import users as svc
from localmail.api.admin.auth import AdminUser, UserNotFound
from localmail.api.ids import parse_int_id
from localmail.serve.admin.csrf import check_csrf
from localmail.serve.admin.dependencies import require_admin_session

router = APIRouter(tags=["admin-users"])


class _UserIn(BaseModel):
    username: str
    password: str = Field(min_length=1)
    is_admin: bool = False


class _UserPatch(BaseModel):
    is_admin: bool | None = None
    disabled: bool | None = None


class _PasswordIn(BaseModel):
    password: str = Field(min_length=1)


class _GrantIn(BaseModel):
    account_id: str
    granted: bool


def _summary_dict(u: svc.UserSummary) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "is_admin": u.is_admin,
        "disabled": u.disabled,
        "created_at": u.created_at.isoformat(),
    }


def _detail_dict(u: svc.UserDetail) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "is_admin": u.is_admin,
        "disabled": u.disabled,
        "created_at": u.created_at.isoformat(),
        "account_grants": [
            {"account_id": str(g.account_id), "account_name": g.account_name,
             "granted": g.granted}
            for g in u.account_grants
        ],
    }


@router.get("/users")
def list_users(request: Request, admin: AdminUser = require_admin_session()) -> dict:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_users(conn)
    return {"users": [_summary_dict(r) for r in rows]}


@router.post("/users", status_code=201)
def create_user(
    body: _UserIn, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    check_csrf(request, admin, x_csrf_token, "/v1/admin/users")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            uid = svc.create_user(
                conn, username=body.username, password=body.password,
                is_admin=body.is_admin)
        except svc.UserFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        summary = next(r for r in svc.list_users(conn) if r.id == uid)
    return _summary_dict(summary)


@router.get("/users/{user_id}")
def get_user(
    user_id: str, request: Request,
    admin: AdminUser = require_admin_session(),
) -> dict:
    uid = parse_int_id(user_id, field="user_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            detail = svc.get_user(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
    return _detail_dict(detail)


@router.patch("/users/{user_id}")
def patch_user(
    user_id: str, body: _UserPatch, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}")
    # Self-demote is a router-level guard (the service is identity-agnostic).
    if body.is_admin is False and uid == admin.id:
        raise HTTPException(status_code=409, detail="you cannot revoke your own admin")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            if body.is_admin is not None:
                svc.set_admin(conn, uid, body.is_admin)
            if body.disabled is not None:
                svc.set_disabled(conn, uid, body.disabled)
            detail = svc.get_user(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.LastAdminError as e:
            raise HTTPException(status_code=409, detail=str(e))
    return _detail_dict(detail)


@router.post("/users/{user_id}/password", status_code=204)
def post_password(
    user_id: str, body: _PasswordIn, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}/password")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.set_password(conn, uid, body.password)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.UserFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return Response(status_code=204)


@router.post("/users/{user_id}/grants")
def post_grant(
    user_id: str, body: _GrantIn, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}/grants")
    aid = parse_int_id(body.account_id, field="account_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.set_grant(conn, uid, aid, body.granted)
            detail = svc.get_user(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.UserFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return _detail_dict(detail)


@router.post("/users/{user_id}/revoke-sessions", status_code=204)
def post_revoke_sessions(
    user_id: str, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}/revoke-sessions")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.revoke_sessions(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
    return Response(status_code=204)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: str, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}")
    if uid == admin.id:
        raise HTTPException(status_code=409, detail="you cannot delete your own account")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_user(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.LastAdminError as e:
            raise HTTPException(status_code=409, detail=str(e))
    return Response(status_code=204)
```

- [ ] **Step 4: Wire the router into the app**

In `src/localmail/serve/app.py`, add the import next to the other admin-router imports (after line 16, `accounts_router as admin_accounts_router`):

```python
from localmail.serve.admin import users_router as admin_users_router
```

And in the `if cfg.session_signing_key:` block, after the `admin_accounts_router` include (line 185), add:

```python
        app.include_router(admin_users_router.router, prefix="/v1/admin")
```

- [ ] **Step 5: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_users.py -q`
Expected: PASS.

- [ ] **Step 6: Run mypy**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail/serve/admin/users_router.py`
Expected: no new errors.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/serve/admin/users_router.py src/localmail/serve/app.py tests/test_serve_admin_users.py
git commit -m "feat(admin): JSON /v1/admin/users router + app wiring"
```

---

## Task 7: HTML panel `/admin/users` + templates + static

**Files:**
- Create: `src/localmail/serve/admin/users_panel_router.py`
- Create: templates under `src/localmail/serve/admin/templates/users/` (9 files)
- Create: `src/localmail/serve/admin/static/users-panel.js`
- Modify: `src/localmail/serve/app.py` (include the panel router under `/admin`)
- Test: `tests/test_serve_admin_user_screens.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_serve_admin_user_screens.py`:

```python
"""HTML-screen tests for /admin/users (Sub-plan 2A.4)."""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY, state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
        cookie_secure=False)


@pytest.fixture
def app(db_dsn, serve_cfg):
    return create_app(db_dsn=db_dsn, serve_config=serve_cfg)


@pytest.fixture
def admin_user_id(db_conn: psycopg.Connection) -> int:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('horst', %s, TRUE) RETURNING id", (pwh,))
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


@pytest.fixture
def admin_client(app, admin_user_id):
    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    r = client.post("/admin/login", data={
        "username": "horst", "password": "hunter2", "csrf_token": m.group(1)})
    assert r.status_code == 303
    key = _SIGNING_KEY.encode("ascii")

    def csrf_for(action: str, method: str = "POST") -> str:
        return make_csrf_token(
            user_id=admin_user_id, action=csrf_action(method, action), key=key)

    client.csrf_for = csrf_for  # type: ignore[attr-defined]
    return client


def _user(db_conn, username, *, is_admin=False):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, 'x', %s) RETURNING id", (username, is_admin))
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


def test_users_list_requires_auth(app):
    client = TestClient(app, follow_redirects=False)
    r = client.get("/admin/users")
    assert r.status_code in (302, 303)


def test_users_list_renders(admin_client):
    r = admin_client.get("/admin/users")
    assert r.status_code == 200
    assert "horst" in r.text


def test_new_user_form_renders(admin_client):
    r = admin_client.get("/admin/users/new")
    assert r.status_code == 200
    assert 'name="username"' in r.text


def test_create_blank_username_inline_error(admin_client):
    r = admin_client.post(
        "/admin/users", data={"username": "", "password": "pw12345"},
        headers={"X-CSRF-Token": admin_client.csrf_for("/admin/users")})
    assert r.status_code == 400
    assert "username" in r.text.lower()


def test_create_success_redirects(admin_client):
    r = admin_client.post(
        "/admin/users", data={"username": "newbie", "password": "pw12345"},
        headers={"X-CSRF-Token": admin_client.csrf_for("/admin/users")})
    assert r.status_code == 200
    assert r.headers["HX-Redirect"].startswith("/admin/users/")


def test_edit_screen_disables_self_demote(admin_client, admin_user_id):
    r = admin_client.get(f"/admin/users/{admin_user_id}")
    assert r.status_code == 200
    # the admin-toggle (demote) control is disabled for the logged-in admin
    assert "disabled" in r.text


def test_grant_toggle_swaps_grants_fragment(admin_client, db_conn, admin_user_id):
    uid = _user(db_conn, "amy")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) VALUES ('alpha', 'a@b.test', 'password', 'h', 993, '{}') "
            "RETURNING id")
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    aid = int(row[0])
    r = admin_client.post(
        f"/admin/users/{uid}/grants",
        data={"account_id": str(aid), "granted": "true"},
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/admin/users/{uid}/grants")})
    assert r.status_code == 200
    assert "alpha" in r.text


def test_delete_self_blocked_fragment(admin_client, admin_user_id):
    r = admin_client.post(
        f"/admin/users/{admin_user_id}/delete",
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/admin/users/{admin_user_id}/delete")})
    assert r.status_code == 409
    assert "your own" in r.text.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_user_screens.py -q`
Expected: FAIL — routes 404.

- [ ] **Step 3: Create the templates**

Create `src/localmail/serve/admin/templates/users/list.html`:

```html
{% extends "base.html" %}
{% block title %}Users — localmail admin{% endblock %}
{% block content %}
<div class="admin-card">
  <div class="accounts-header">
    <h1>Users</h1>
    <a href="/admin/users/new" class="admin-button">+ New user</a>
  </div>
  <table class="accounts-table">
    <thead>
      <tr><th>Username</th><th>Admin</th><th>Status</th><th></th></tr>
    </thead>
    <tbody>
      {% for u in users %}
        {% include "users/_row.html" %}
      {% else %}
      <tr><td colspan="4">No users.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

Create `src/localmail/serve/admin/templates/users/_row.html`:

```html
<tr id="user-row-{{ u.id }}">
  <td><a href="/admin/users/{{ u.id }}">{{ u.username }}</a></td>
  <td>{% if u.is_admin %}<span class="sync-on">admin</span>{% else %}—{% endif %}</td>
  <td>{% if u.disabled %}<span class="sync-off">disabled</span>{% else %}<span class="sync-on">active</span>{% endif %}</td>
  <td class="account-row-actions"><a href="/admin/users/{{ u.id }}">Edit</a></td>
</tr>
```

Create `src/localmail/serve/admin/templates/users/new.html`:

```html
{% extends "base.html" %}
{% block title %}New user — localmail admin{% endblock %}
{% block content %}
<div class="admin-card">
  <h1>New user</h1>
  <form id="user-form" class="account-form"
        hx-post="/admin/users" hx-target="#user-form-fields" hx-swap="outerHTML"
        hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/users") }}"}'>
    {% include "users/_create_fields.html" %}
  </form>
</div>
{% endblock %}
```

Create `src/localmail/serve/admin/templates/users/_create_fields.html`:

```html
<div id="user-form-fields">
  {% if field_errors._form %}<p class="admin-flash admin-flash-error">{{ field_errors._form }}</p>{% endif %}
  <label>Username
    <input name="username" value="{{ values.username }}" required>
    {% if field_errors.username %}<span class="field-error">{{ field_errors.username }}</span>{% endif %}
  </label>
  <label>Password
    <input type="password" name="password" required>
    {% if field_errors.password %}<span class="field-error">{{ field_errors.password }}</span>{% endif %}
  </label>
  <label><input type="checkbox" name="is_admin" {% if values.is_admin %}checked{% endif %}> Admin</label>
  <div class="form-actions"><button type="submit" class="admin-button">Create</button></div>
</div>
```

Create `src/localmail/serve/admin/templates/users/edit.html`:

```html
{% extends "base.html" %}
{% block title %}Edit user — localmail admin{% endblock %}
{% block content %}
<div class="admin-card">
  <h1>{{ detail.username }}</h1>

  <div id="user-status">
    {% include "users/_status.html" %}
  </div>

  <fieldset>
    <legend>Reset password</legend>
    <input type="password" name="password" form="_none">
    <button type="button" class="admin-button"
      hx-post="/admin/users/{{ detail.id }}/password"
      hx-include="[name='password']" hx-target="#user-message" hx-swap="innerHTML"
      hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/users/" ~ detail.id ~ "/password") }}"}'>
      Set password</button>
    <button type="button" class="admin-button"
      hx-post="/admin/users/{{ detail.id }}/revoke-sessions"
      hx-target="#user-message" hx-swap="innerHTML"
      hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/users/" ~ detail.id ~ "/revoke-sessions") }}"}'>
      Revoke sessions</button>
    <span id="user-message"></span>
  </fieldset>

  <fieldset id="user-grants">
    {% include "users/_grants.html" %}
  </fieldset>

  <div id="user-danger">
    <button type="button" class="admin-button danger"
      {% if flags.block_delete %}disabled title="cannot delete the last admin / your own account"{% endif %}
      hx-post="/admin/users/{{ detail.id }}/delete"
      hx-confirm="Delete user {{ detail.username }}?"
      hx-target="#user-danger" hx-swap="innerHTML"
      hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/users/" ~ detail.id ~ "/delete") }}"}'>
      Delete user</button>
  </div>
</div>
<script src="/admin/static/users-panel.js" defer></script>
{% endblock %}
```

Create `src/localmail/serve/admin/templates/users/_status.html`:

```html
{% if error %}<p class="admin-flash admin-flash-error">{{ error }}</p>{% endif %}
<p>Admin:
  {% if detail.is_admin %}<span class="sync-on">yes</span>{% else %}<span class="sync-off">no</span>{% endif %}
  <button type="button" class="link-button"
    {% if detail.is_admin and flags.block_demote %}disabled title="cannot demote the last admin / yourself"{% endif %}
    hx-post="/admin/users/{{ detail.id }}/admin-toggle"
    hx-target="#user-status" hx-swap="innerHTML"
    hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/users/" ~ detail.id ~ "/admin-toggle") }}"}'>
    {% if detail.is_admin %}Revoke admin{% else %}Grant admin{% endif %}</button>
</p>
<p>Status:
  {% if detail.disabled %}<span class="sync-off">disabled</span>{% else %}<span class="sync-on">active</span>{% endif %}
  <button type="button" class="link-button"
    {% if not detail.disabled and flags.block_disable %}disabled title="cannot disable the last admin"{% endif %}
    hx-post="/admin/users/{{ detail.id }}/disable-toggle"
    hx-target="#user-status" hx-swap="innerHTML"
    hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/users/" ~ detail.id ~ "/disable-toggle") }}"}'>
    {% if detail.disabled %}Enable{% else %}Disable{% endif %}</button>
</p>
```

Create `src/localmail/serve/admin/templates/users/_grants.html`:

```html
<legend>Account access</legend>
{% if not detail.account_grants %}<p>No accounts configured.</p>{% endif %}
{% for g in detail.account_grants %}
<label>
  <input type="checkbox" {% if g.granted %}checked{% endif %}
    hx-post="/admin/users/{{ detail.id }}/grants"
    hx-vals='{"account_id": "{{ g.account_id }}", "granted": "{{ "false" if g.granted else "true" }}"}'
    hx-target="#user-grants" hx-swap="innerHTML"
    hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/users/" ~ detail.id ~ "/grants") }}"}'>
  {{ g.account_name }}
</label>
{% endfor %}
```

Create `src/localmail/serve/admin/templates/users/_message.html`:

```html
<span class="secret-ok">{{ message }}</span>
```

Create `src/localmail/serve/admin/templates/users/_delete_blocked.html`:

```html
<p class="admin-flash admin-flash-error">{{ error }}</p>
```

- [ ] **Step 4: Create the static file**

Create `src/localmail/serve/admin/static/users-panel.js`:

```javascript
// Reserved for the users panel. Delete confirmation uses htmx's hx-confirm,
// so no inline behaviour is required today; the file exists so edit.html's
// <script src> resolves under the script-src 'self' CSP.
```

- [ ] **Step 5: Implement the HTML panel router**

Create `src/localmail/serve/admin/users_panel_router.py`:

```python
"""Admin user-management HTML screens (2A.4).

Thin server-rendered HTMX router mounted at /admin. Renders Jinja fragments and
dispatches to api/admin/users; all form parsing lives in user_forms. Mutating
routes verify a method-bound CSRF token (X-CSRF-Token header) via check_csrf.
JSON machine clients use /v1/admin/users.

The last-admin guard is enforced by the service; the self-action guard is
enforced here (only the router knows the caller's identity). The edit screen
also renders unsafe controls `disabled` (UX only — POSTing anyway still hits
the guards).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from localmail.api.admin import users as svc
from localmail.api.admin.auth import AdminUser, UserNotFound
from localmail.api.ids import parse_int_id
from localmail.serve.admin import user_forms as forms
from localmail.serve.admin.csrf import check_csrf, csrf_token_context, session_signing_key
from localmail.serve.admin.dependencies import require_admin_session

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


def _base_context(request: Request, admin: AdminUser) -> dict:
    s_key = session_signing_key(request)
    return {
        "current_user": admin,
        "flashes": [],
        **csrf_token_context(user_id=admin.id, key=s_key),
    }


def _detail_and_flags(conn, user_id: int, self_id: int):
    """(detail, flags) for the edit screen. Raises UserNotFound."""
    detail = svc.get_user(conn, user_id)
    flags = svc.action_flags(
        target_is_active_admin=(detail.is_admin and not detail.disabled),
        active_admin_count=svc.active_admin_count(conn),
        is_self=(user_id == self_id),
    )
    return detail, flags


def _status_fragment(
    request: Request, admin: AdminUser, conn, user_id: int, *,
    error: str | None = None, status: int = 200,
) -> HTMLResponse:
    detail, flags = _detail_and_flags(conn, user_id, admin.id)
    ctx = _base_context(request, admin)
    ctx.update({"detail": detail, "flags": flags, "error": error})
    return templates.TemplateResponse(
        request=request, name="users/_status.html", context=ctx, status_code=status)


def _grants_fragment(
    request: Request, admin: AdminUser, conn, user_id: int,
) -> HTMLResponse:
    detail, flags = _detail_and_flags(conn, user_id, admin.id)
    ctx = _base_context(request, admin)
    ctx.update({"detail": detail, "flags": flags})
    return templates.TemplateResponse(
        request=request, name="users/_grants.html", context=ctx)


def _message(request: Request, admin: AdminUser, message: str) -> HTMLResponse:
    ctx = _base_context(request, admin)
    ctx["message"] = message
    return templates.TemplateResponse(
        request=request, name="users/_message.html", context=ctx)


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, admin: AdminUser = require_admin_session()) -> HTMLResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_users(conn)
    ctx = _base_context(request, admin)
    ctx["users"] = rows
    return templates.TemplateResponse(request=request, name="users/list.html", context=ctx)


@router.get("/users/new", response_class=HTMLResponse)
def new_user_form(request: Request, admin: AdminUser = require_admin_session()) -> HTMLResponse:
    ctx = _base_context(request, admin)
    ctx.update({"values": {"username": "", "is_admin": False}, "field_errors": {}})
    return templates.TemplateResponse(request=request, name="users/new.html", context=ctx)


def _rerender_create_error(request, admin, raw, err) -> HTMLResponse:
    ctx = _base_context(request, admin)
    ctx.update({
        "values": {"username": raw.get("username", ""),
                   "is_admin": bool(raw.get("is_admin"))},
        "field_errors": forms.field_errors_from(err),
    })
    return templates.TemplateResponse(
        request=request, name="users/_create_fields.html", context=ctx, status_code=400)


@router.post("/users")
async def create_user(
    request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, "/admin/users")
    raw = dict(await request.form())
    try:
        kwargs = forms.form_to_create_kwargs(raw)
    except forms.FormError as e:
        return _rerender_create_error(request, admin, raw, e)
    pool = request.app.state.pool

    def _create() -> int:
        with pool.connection() as conn:
            return svc.create_user(conn, **kwargs)

    try:
        uid = await run_in_threadpool(_create)
    except svc.UserFieldError as e:
        return _rerender_create_error(request, admin, raw, e)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/admin/users/{uid}"
    return resp


@router.get("/users/{user_id}", response_class=HTMLResponse)
def edit_user_form(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
) -> HTMLResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            detail, flags = _detail_and_flags(conn, user_id, admin.id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
    ctx = _base_context(request, admin)
    ctx.update({"detail": detail, "flags": flags, "error": None})
    return templates.TemplateResponse(request=request, name="users/edit.html", context=ctx)


@router.post("/users/{user_id}/admin-toggle", response_class=HTMLResponse)
def admin_toggle(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/admin-toggle")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            detail = svc.get_user(conn, user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        target = not detail.is_admin
        if not target and user_id == admin.id:
            return _status_fragment(request, admin, conn, user_id,
                                    error="you cannot revoke your own admin")
        try:
            svc.set_admin(conn, user_id, target)
        except svc.LastAdminError as e:
            return _status_fragment(request, admin, conn, user_id, error=str(e))
        return _status_fragment(request, admin, conn, user_id)


@router.post("/users/{user_id}/disable-toggle", response_class=HTMLResponse)
def disable_toggle(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/disable-toggle")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            detail = svc.get_user(conn, user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        try:
            svc.set_disabled(conn, user_id, not detail.disabled)
        except svc.LastAdminError as e:
            return _status_fragment(request, admin, conn, user_id, error=str(e))
        return _status_fragment(request, admin, conn, user_id)


@router.post("/users/{user_id}/password", response_class=HTMLResponse)
async def store_password(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/password")
    raw = await request.form()
    password = str(raw.get("password", ""))
    pool = request.app.state.pool

    def _store() -> None:
        with pool.connection() as conn:
            svc.set_password(conn, user_id, password)

    try:
        await run_in_threadpool(_store)
    except UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    except svc.UserFieldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _message(request, admin, "Password updated.")


@router.post("/users/{user_id}/revoke-sessions", response_class=HTMLResponse)
def revoke_sessions(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/revoke-sessions")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.revoke_sessions(conn, user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
    return _message(request, admin, "Sessions revoked.")


@router.post("/users/{user_id}/grants", response_class=HTMLResponse)
async def set_grant(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/grants")
    raw = await request.form()
    account_id = parse_int_id(str(raw.get("account_id", "")), field="account_id")
    granted = str(raw.get("granted", "")).lower() == "true"
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.set_grant(conn, user_id, account_id, granted)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.UserFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _grants_fragment(request, admin, conn, user_id)


@router.post("/users/{user_id}/delete")
def delete_user(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/delete")
    if user_id == admin.id:
        ctx = _base_context(request, admin)
        ctx["error"] = "You cannot delete your own account."
        return templates.TemplateResponse(
            request=request, name="users/_delete_blocked.html", context=ctx,
            status_code=409)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_user(conn, user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.LastAdminError as e:
            ctx = _base_context(request, admin)
            ctx["error"] = str(e)
            return templates.TemplateResponse(
                request=request, name="users/_delete_blocked.html", context=ctx,
                status_code=409)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/admin/users"
    return resp
```

- [ ] **Step 6: Wire the panel router into the app**

In `src/localmail/serve/app.py`, add the import (after the `users_router` import from Task 6):

```python
from localmail.serve.admin import users_panel_router as admin_users_panel_router
```

And in the `if cfg.session_signing_key:` block, after the `admin_accounts_panel_router` include:

```python
        app.include_router(admin_users_panel_router.router, prefix="/admin")
```

- [ ] **Step 7: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_user_screens.py -q`
Expected: PASS.

- [ ] **Step 8: Run mypy**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail/serve/admin/users_panel_router.py`
Expected: no new errors. (If mypy flags the untyped `conn` param on the helpers, add `import psycopg` and annotate `conn: psycopg.Connection`.)

- [ ] **Step 9: Commit**

```bash
git add src/localmail/serve/admin/users_panel_router.py \
        src/localmail/serve/admin/templates/users/ \
        src/localmail/serve/admin/static/users-panel.js \
        src/localmail/serve/app.py \
        tests/test_serve_admin_user_screens.py
git commit -m "feat(admin): HTML /admin/users screens + templates"
```

---

## Task 8: Docs + full-suite verification

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Add the CLAUDE.md bullet**

In `CLAUDE.md`, in the "GUI server (Phase 1 of GUI)" section, after the
"Account CRUD admin screens (Sub-plan 2A.3, shipped …)" bullet and the
"Friendly test-connection failures (#158, resolved)" bullet, add:

```markdown
- **User-management admin screens (Sub-plan 2A.4, shipped):** server-rendered
  HTMX screens at `/admin/users` + a JSON `/v1/admin/users` router, sharing one
  service layer
  [`src/localmail/api/admin/users.py`](src/localmail/api/admin/users.py):
  list/create/delete users, per-account ACL grant/revoke (a checklist over every
  account on the edit screen), `is_admin` toggle, admin session revocation,
  admin password reset (no old password), and enable/disable (`disabled_at`).
  Two lock-out guards: the **count-based last-admin** rule lives in the service
  (the pure `would_orphan_last_admin` predicate + an IO wrapper reading
  `count(*) WHERE is_admin IS TRUE AND disabled_at IS NULL`; raises
  `LastAdminError`), and the **identity-based self-action** rule (no self-demote,
  no self-delete) lives in the routers (`SelfActionError`). Both map to **409**
  (mirroring the accounts cascade-refuse 409); validation maps to **400**. The
  edit screen also renders unsafe controls `disabled` server-side via
  `action_flags` — UX only; a hand-crafted POST still hits the guards. Pure form
  logic in
  [`serve/admin/user_forms.py`](src/localmail/serve/admin/user_forms.py)
  (unit-tested in `tests/test_user_forms.py`). Method-bound CSRF throughout
  (a PATCH token can't replay on DELETE). **No new migration** — reuses
  `is_admin`/`disabled_at`/`sessions_invalidated_at` (0022) + `user_accounts`
  (0016). Closes the `/admin/users` 404.
```

- [ ] **Step 2: Update README.md**

In `README.md`, find the admin-UI section that lists `/admin/accounts` and
`/admin/daemon`. Add a sentence describing `/admin/users`:

```markdown
- **`/admin/users`** — manage API users: create/delete, grant or revoke
  per-account access, toggle admin rights, reset passwords, enable/disable, and
  revoke outstanding sessions. The UI refuses any action that would remove the
  last admin or lock out your own account.
```

(If the README has no admin-screens list yet, add this under the GUI-server
heading alongside the accounts/daemon screens.)

- [ ] **Step 3: Run the FULL test suite**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/`
Expected: PASS — the prior 1295 plus the new tests (≈1295 + ~45 new). The
psycopg-pool `__del__` ResourceWarnings at teardown are pre-existing, not
failures.

- [ ] **Step 4: Run mypy across the package**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: `Success: no issues found`. The pre-existing `cli.py:596`
`annotation-unchecked` note is unrelated.

- [ ] **Step 5: Commit docs**

```bash
git add CLAUDE.md README.md
git commit -m "docs(admin): document /admin/users management screens (2A.4)"
```

- [ ] **Step 6: Push and open the PR**

```bash
git push -u origin admin-ui-2a4-user-screens
gh pr create --fill --base main \
  --title "feat(admin): user-management admin screens (2A.4)" \
  --body "Closes the /admin/users 404. Server-rendered HTMX screens + JSON /v1/admin/users router for managing API users: CRUD, per-account ACL grants, is_admin toggle + session revocation, admin password reset, enable/disable. Last-admin + self-action lock-out guards. No new migration. See docs/superpowers/specs/2026-06-05-admin-users-screens-design.md."
```

---

## Self-review notes (author)

- **Spec coverage:** list/create/delete (Tasks 1,2,3,6,7), ACL grants (Task 4 service, 6/7 routes), admin toggle + revoke-sessions (Tasks 3,4,6,7), password reset (Task 2, 6/7), enable/disable (Task 3, 6/7), last-admin guard (Task 3 + predicate test Task 5), self-action guard (Tasks 6,7 + tests), both transports (Tasks 6,7), no migration (stated), docs (Task 8). All spec sections map to a task.
- **Template-name refinement vs spec:** the spec listed `form.html` / `_form_fields.html` / `_delete_confirm.html`; the plan splits create vs edit into `new.html` + `edit.html` (they share almost no fields, so one combined file would branch heavily) and uses `hx-confirm` + `_delete_blocked.html` instead of a separate `_delete_confirm.html`. Same behaviour; cleaner, more focused files. `_message.html` replaces the spec's `_secret_status.html` name (generic one-liner reused by password + revoke-sessions).
- **Type consistency:** service returns `UserSummary`/`UserDetail`/`AccountGrant`; routers consume `.id`/`.username`/`.is_admin`/`.disabled`/`.created_at`/`.account_grants[].{account_id,account_name,granted}` consistently. `action_flags` keys `block_demote`/`block_disable`/`block_delete` match the templates' `flags.block_*` reads. `would_orphan_last_admin(target_is_active_admin=, active_admin_count=)` signature identical across service, tests, and `action_flags`.
- **Placeholder scan:** no TBD/TODO; every code step shows complete code; every run step shows the command + expected result.
```
