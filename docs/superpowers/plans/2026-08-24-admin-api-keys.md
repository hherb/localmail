# Admin-issued API Keys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin mint a named, never-expiring API key for a machine consumer, which authenticates `/v1/*` and `/mcp` but never an admin route.

**Architecture:** A key is an `api_tokens` row with `api_key_name` set and `expires_at NULL`, minted against a dedicated **service user** (`api_users.is_service`). Because the principal is an ordinary `api_users` row, the per-account ACL and all three existing revocation levers reach the key with no changes. Two new rules are added: `require_admin()` refuses an API-key bearer, and a service user cannot log in through any of the three password-verifying sites.

**Tech Stack:** Python ≥3.12, `uv`, psycopg 3 with raw SQL, FastAPI + Jinja2 + HTMX, click, pytest against a real Postgres.

**Spec:** [docs/superpowers/specs/2026-08-24-admin-api-keys-design.md](../specs/2026-08-24-admin-api-keys-design.md)

## Global Constraints

- Branch is `feat/admin-api-keys`, already created off `main`. Do not push to `main`.
- Run everything as `unset VIRTUAL_ENV && uv run …` — a stray `VIRTUAL_ENV` makes `uv` pick the wrong interpreter.
- If you ever re-sync: `uv sync --extra extraction --extra mcp`. Omitting the extras uninstalls `docling`, `mcp` and `rapidocr`. **Never** run `uv sync --dry-run` — on uv 0.11.32 it replaces the project environment.
- **No ORM.** Raw SQL through psycopg 3 only.
- **No comments unless the WHY is non-obvious.** Never restate the SQL or the Python.
- Annotate every new DB helper's connection parameter as `conn: psycopg.Connection`. On an unannotated `conn` the cursor is `Any` and mypy silently misses `cur.fetchone()[0]` violations.
- After any `cur.fetchone()`, `assert row is not None` before subscripting.
- **Never edit an applied migration.** The only new one here is `0036_api_keys.sql`; the next free slot after it is `0037_*.sql`.
- Tests run against `LOCALMAIL_TEST_DSN`, which defaults to the **`localmail_test`** database. Never point it at the live `localmail` DB.
- IDs are **strings on the wire** in both directions; route handlers call `api.ids.parse_int_id` exactly once per ID.
- Every mutating admin route verifies a **method-bound** CSRF token via `check_csrf`.
- Full suite command, used at the end of every task: `unset VIRTUAL_ENV && uv run pytest -q`

---

## File Structure

| Path | New? | Responsibility |
| --- | --- | --- |
| `migrations/0036_api_keys.sql` | new | the three DB invariants |
| `src/localmail/api/login_eligible_sql.py` | new | pure: the one "may this principal log in" fragment |
| `src/localmail/api/admin/api_key_names.py` | new | pure: `api_key_name_error` |
| `src/localmail/api/admin/api_keys.py` | new | service layer: list / create / revoke / delete |
| `src/localmail/api/auth.py` | modify | `AuthenticatedUser.is_api_key`, `verify_token` predicate, `login` splice |
| `src/localmail/api/admin/auth.py` | modify | `authenticate_admin` splice |
| `src/localmail/serve/oauth/consent_router.py` | modify | consent-login splice |
| `src/localmail/api/admin/users.py` | modify | refuse password-reset / admin-toggle on a service row |
| `src/localmail/serve/admin/dependencies.py` | modify | Rule 1: refuse API-key bearers |
| `src/localmail/serve/admin/api_key_forms.py` | new | pure form parsing |
| `src/localmail/serve/admin/api_keys_router.py` | new | JSON `/v1/admin/api-keys` |
| `src/localmail/serve/admin/api_keys_panel_router.py` | new | HTML `/admin/api-keys` |
| `src/localmail/serve/admin/templates/api_keys/*.html` | new | panel templates |
| `src/localmail/serve/admin/static/api-keys-panel.js` | new | copy button (CSP forbids inline JS) |
| `src/localmail/serve/admin/templates/base.html` | modify | nav link |
| `src/localmail/serve/admin/templates/dashboard.html` | modify | dashboard card |
| `src/localmail/serve/app.py` | modify | mount both routers |
| `src/localmail/cli.py` | modify | four commands |

---

### Task 1: Migration — the DB invariants

**Files:**
- Create: `migrations/0036_api_keys.sql`
- Test: `tests/test_migration_api_keys.py`

**Interfaces:**
- Consumes: nothing.
- Produces: columns `api_tokens.api_key_name TEXT NULL`, `api_users.is_service BOOLEAN NOT NULL DEFAULT FALSE`; nullable `api_tokens.expires_at`; constraint `api_tokens_only_keys_are_immortal`; unique index `api_tokens_one_key_per_service_user`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_api_keys.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Migration 0036's invariants, which no Python code can route around."""
from __future__ import annotations

import hashlib

import psycopg
import pytest


def _user(conn: psycopg.Connection, username: str, *, is_service: bool = False) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_service) "
            "VALUES (%s, 'x', %s) RETURNING id",
            (username, is_service),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _sha(s: str) -> bytes:
    return hashlib.sha256(s.encode()).digest()


def test_an_api_key_may_have_no_expiry(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, 'bot')",
            (_sha("k1"), uid),
        )
    db_conn.commit()


def test_a_session_token_may_not_have_no_expiry(db_conn):
    """The load-bearing half: dropping NOT NULL alone would allow an immortal
    login token, produced by a one-line bug, with nothing failing."""
    uid = _user(db_conn, "human")
    with pytest.raises(psycopg.errors.CheckViolation):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_tokens (token_sha256, user_id, expires_at) "
                "VALUES (%s, %s, NULL)",
                (_sha("k2"), uid),
            )
    db_conn.rollback()


def test_one_key_per_service_user(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, 'bot')",
            (_sha("k1"), uid),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
                "VALUES (%s, %s, NULL, 'bot-2')",
                (_sha("k3"), uid),
            )
    db_conn.rollback()


def test_a_user_may_hold_many_session_tokens(db_conn):
    """The unique index is partial; it must not constrain ordinary tokens."""
    uid = _user(db_conn, "human")
    with db_conn.cursor() as cur:
        for i in range(3):
            cur.execute(
                "INSERT INTO api_tokens (token_sha256, user_id, expires_at) "
                "VALUES (%s, %s, now() + interval '1 day')",
                (_sha(f"s{i}"), uid),
            )
    db_conn.commit()


def test_is_service_defaults_false(db_conn):
    uid = _user(db_conn, "human")
    with db_conn.cursor() as cur:
        cur.execute("SELECT is_service FROM api_users WHERE id = %s", (uid,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_migration_api_keys.py -q`
Expected: FAIL — `UndefinedColumn: column "api_key_name" of relation "api_tokens" does not exist`.

- [ ] **Step 3: Write the migration**

```sql
-- migrations/0036_api_keys.sql
-- Admin-issued API keys: a named, never-expiring credential for a machine
-- consumer, minted against a dedicated service user.
--
-- `api_key_name IS NOT NULL` IS the credential kind. There is deliberately no
-- second boolean beside it that could disagree. The column is `api_key_name`
-- rather than `name` so that a future "let users label their sessions" feature
-- must add its own column instead of inheriting API-key semantics -- an
-- immortal credential barred from admin routes -- by writing to a field that
-- merely sounds general.
--
-- Dropping NOT NULL from expires_at on its own would let a *login* token be
-- minted with no expiry: an immortal interactive credential, produced by a
-- one-line bug, with nothing failing and no query that would look wrong. The
-- CHECK scopes "may live forever" to API keys, here, where no code path routes
-- around it.
--
-- The unique index is keyed on user_id alone, not (user_id, api_key_name): the
-- pair would permit several differently-named keys on one principal, which is
-- the many-keys model the design defers. Key names are unique globally for
-- free, via the existing api_users.username unique constraint.
--
-- Lock cost: all three ALTERs are metadata-only in Postgres 11+ (ADD COLUMN
-- nullable, ADD COLUMN with a constant default, DROP NOT NULL). The CHECK is
-- validated against existing rows, and the index build takes a brief write
-- lock; api_tokens holds one row per live session, so both are trivial.

ALTER TABLE api_tokens
    ADD COLUMN IF NOT EXISTS api_key_name TEXT;

ALTER TABLE api_tokens
    ALTER COLUMN expires_at DROP NOT NULL;

ALTER TABLE api_tokens DROP CONSTRAINT IF EXISTS api_tokens_only_keys_are_immortal;
ALTER TABLE api_tokens ADD  CONSTRAINT api_tokens_only_keys_are_immortal
    CHECK (api_key_name IS NOT NULL OR expires_at IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS api_tokens_one_key_per_service_user
    ON api_tokens (user_id)
    WHERE api_key_name IS NOT NULL;

ALTER TABLE api_users
    ADD COLUMN IF NOT EXISTS is_service BOOLEAN NOT NULL DEFAULT FALSE;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_migration_api_keys.py -q`
Expected: 5 passed. The `db_dsn` fixture applies pending migrations once per session.

- [ ] **Step 5: Commit**

```bash
git add migrations/0036_api_keys.sql tests/test_migration_api_keys.py
git commit -m "feat(db): API-key columns and the invariants that scope them (0036)"
```

---

### Task 2: `verify_token` — accept immortal keys, report `is_api_key`

**Files:**
- Modify: `src/localmail/api/auth.py` (the `AuthenticatedUser` dataclass and `verify_token`)
- Test: `tests/test_api_key_verify.py`

**Interfaces:**
- Consumes: Task 1's `api_key_name` column and nullable `expires_at`.
- Produces: `AuthenticatedUser(id: int, username: str, is_admin: bool = False, *, is_api_key: bool)` — `is_api_key` is keyword-only and **has no default**. Every later task reads `user.is_api_key`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_key_verify.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""verify_token over the two credential kinds: an immortal API key and an
ordinary session token."""
from __future__ import annotations

import psycopg

from localmail.api.auth import hash_token, issue_token, verify_token


def _user(conn: psycopg.Connection, username: str, *, is_service: bool = False) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_service) "
            "VALUES (%s, 'x', %s) RETURNING id",
            (username, is_service),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _mint_key(conn: psycopg.Connection, uid: int, name: str, raw: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, %s)",
            (hash_token(raw), uid, name),
        )


def test_a_key_with_no_expiry_authenticates(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    db_conn.commit()
    user = verify_token(db_conn, "lmk_raw")
    assert user is not None
    assert user.id == uid
    assert user.is_api_key is True


def test_a_session_token_reports_is_api_key_false(db_conn):
    uid = _user(db_conn, "human")
    tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    user = verify_token(db_conn, tok)
    assert user is not None
    assert user.is_api_key is False


def test_an_expired_session_token_is_still_rejected(db_conn):
    uid = _user(db_conn, "human")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at) "
            "VALUES (%s, %s, now() - interval '1 second')",
            (hash_token("stale"), uid),
        )
    db_conn.commit()
    assert verify_token(db_conn, "stale") is None


def test_session_revocation_kills_the_key(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET sessions_invalidated_at = now() + interval '1 second' "
            "WHERE id = %s",
            (uid,),
        )
    db_conn.commit()
    assert verify_token(db_conn, "lmk_raw") is None


def test_disabling_the_principal_kills_the_key(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET disabled_at = now() WHERE id = %s", (uid,))
    db_conn.commit()
    assert verify_token(db_conn, "lmk_raw") is None


def test_deleting_the_token_row_kills_the_key(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM api_tokens WHERE user_id = %s", (uid,))
    db_conn.commit()
    assert verify_token(db_conn, "lmk_raw") is None


def test_the_mcp_verifier_accepts_a_key(db_conn):
    """/mcp is one of the two surfaces a key exists to reach."""
    import anyio

    from localmail.mcp.auth import LocalmailTokenVerifier

    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    db_conn.commit()

    class _OneConnPool:
        def connection(self):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield db_conn

            return _cm()

    verifier = LocalmailTokenVerifier(_OneConnPool())  # type: ignore[arg-type]
    access = anyio.run(verifier.verify_token, "lmk_raw")
    assert access is not None
    assert access.subject == str(uid)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_key_verify.py -q`
Expected: FAIL — `AttributeError: 'AuthenticatedUser' object has no attribute 'is_api_key'`.

- [ ] **Step 3: Write the implementation**

In `src/localmail/api/auth.py`, extend the dataclass import and the class. The existing import line is `from dataclasses import dataclass`; make it:

```python
from dataclasses import dataclass, field
```

Replace the `AuthenticatedUser` class body:

```python
@dataclass(frozen=True)
class AuthenticatedUser:
    """The user behind a valid bearer token."""
    id: int
    username: str
    is_admin: bool = False
    # Keyword-only with no default: False is the *permissive* value here — it
    # means "allowed at admin routes" — so it must not be reachable by
    # forgetting to write it.
    is_api_key: bool = field(kw_only=True)
```

Replace `verify_token`'s SELECT and its return, keeping the existing docstring and the `last_used_at` UPDATE untouched:

```python
    h = hash_token(token)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.username, u.is_admin, (t.api_key_name IS NOT NULL) "
            "FROM api_tokens t "
            "JOIN api_users u ON u.id = t.user_id "
            "WHERE t.token_sha256 = %s "
            # NULL expires_at is an API key, which never expires; the CHECK in
            # migration 0036 is what keeps a session token from reaching it.
            "  AND (t.expires_at IS NULL OR t.expires_at > now()) "
            # Session revocation (revoke-sessions / revoke-admin-sessions) bumps
            # sessions_invalidated_at to "now"; every bearer token issued before
            # that moment must stop authenticating, matching the admin-cookie
            # path (require_admin_session). Without this, a leaked 30-day bearer
            # token survived a revocation the operator believed cut off access.
            "  AND " + credential_valid_sql(user="u", credential="t"),
            (h,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE api_tokens SET last_used_at = now() "
            "WHERE token_sha256 = %s "
            "  AND (last_used_at IS NULL "
            "       OR last_used_at < now() - make_interval(secs => %s))",
            (h, LAST_USED_REFRESH_SECONDS),
        )
    return AuthenticatedUser(
        id=row[0], username=row[1], is_admin=bool(row[2]), is_api_key=bool(row[3]),
    )
```

Also update the docstring's first line to mention the new kind:

```python
    """Look up a bearer token or an API key; return the user, or None when the
    credential is invalid, expired, revoked (``sessions_invalidated_at``), or the
    user is disabled. An API key carries ``expires_at IS NULL`` and never
    expires.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_key_verify.py -q`
Expected: 7 passed.

Then confirm nothing else constructed `AuthenticatedUser`:

Run: `unset VIRTUAL_ENV && uv run pytest -q`
Expected: the whole suite passes. If anything fails with `TypeError: missing 1 required keyword-only argument: 'is_api_key'`, that call site must pass the value explicitly — do **not** give the field a default.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/auth.py tests/test_api_key_verify.py
git commit -m "feat(auth): verify immortal API keys and report the credential kind"
```

---

### Task 3: Rule 2 — a service user cannot log in

**Files:**
- Create: `src/localmail/api/login_eligible_sql.py`
- Modify: `src/localmail/api/auth.py` (`login`), `src/localmail/api/admin/auth.py` (`authenticate_admin`), `src/localmail/serve/oauth/consent_router.py`, `src/localmail/api/admin/users.py` (`set_password`, `set_admin`)
- Test: `tests/test_service_user_cannot_log_in.py`

**Interfaces:**
- Consumes: Task 1's `api_users.is_service`.
- Produces: `login_eligible_sql(*, user: str) -> str` — a parameter-free SQL boolean over one `api_users` alias, already parenthesised. `users.UserFieldError` is raised for a service-row password reset or admin promotion.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_service_user_cannot_log_in.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A service user is a machine principal; no password path may admit it.

One test per verifying site, deliberately not parametrised over the shared
fragment: the drift the fragment exists to prevent is a site that does not use
it, which a test calling the fragment directly cannot see.
"""
from __future__ import annotations

import psycopg
import pytest

from localmail.api import auth as api_auth
from localmail.api.admin import auth as admin_auth
from localmail.api.admin import users as users_svc
from localmail.api.errors import AuthenticationFailed
from localmail.api.login_eligible_sql import login_eligible_sql

_PW = "correct-horse"


def _service_user(conn: psycopg.Connection, username: str = "bot") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_service, is_admin) "
            "VALUES (%s, %s, TRUE, TRUE) RETURNING id",
            (username, api_auth.hash_password(_PW)),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_fragment_is_parenthesised_and_names_its_alias():
    sql = login_eligible_sql(user="u")
    assert sql.startswith("(") and sql.endswith(")")
    assert "u.disabled_at IS NULL" in sql
    assert "u.is_service IS FALSE" in sql


def test_v1_auth_login_refuses_a_service_user(db_conn):
    _service_user(db_conn)
    with pytest.raises(AuthenticationFailed):
        api_auth.login(db_conn, "bot", _PW)


def test_admin_cookie_login_refuses_a_service_user(db_conn):
    _service_user(db_conn)
    with pytest.raises(AuthenticationFailed):
        admin_auth.authenticate_admin(db_conn, "bot", _PW)


def test_oauth_consent_login_refuses_a_service_user(db_conn):
    """The consent router verifies its own password lookup inline."""
    _service_user(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM api_users WHERE username = %s AND "
            + login_eligible_sql(user="api_users"),
            ("bot",),
        )
        assert cur.fetchone() is None
    import inspect

    from localmail.serve.oauth import consent_router

    assert "login_eligible_sql" in inspect.getsource(consent_router)


def test_a_human_still_logs_in(db_conn):
    """Positive control: the fragment must not lock everyone out."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES (%s, %s)",
            ("human", api_auth.hash_password(_PW)),
        )
    db_conn.commit()
    token, _ = api_auth.login(db_conn, "human", _PW)
    assert token


def test_password_reset_is_refused_on_a_service_row(db_conn):
    """Without this the Users panel hands a bot an interactive login — the one
    path that makes the unusable password hash usable again."""
    uid = _service_user(db_conn)
    with pytest.raises(users_svc.UserFieldError):
        users_svc.set_password(db_conn, uid, "new-password")


def test_admin_promotion_is_refused_on_a_service_row(db_conn):
    uid = _service_user(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET is_admin = FALSE WHERE id = %s", (uid,))
    db_conn.commit()
    with pytest.raises(users_svc.UserFieldError):
        users_svc.set_admin(db_conn, uid, True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_service_user_cannot_log_in.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.api.login_eligible_sql'`.

- [ ] **Step 3: Write the implementation**

Create `src/localmail/api/login_eligible_sql.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one SQL fragment deciding whether a principal may present a password.

Pure — string composition only, no IO, no psycopg. A sibling of
``revocation_sql.credential_valid_sql``, and for the same reason: three separate
lookups verify a password against ``api_users``, and until this module they
carried the ``disabled_at IS NULL`` wording by copy. #241 was exactly a rule
applied to one site and not its sibling.

A **service user** is the principal behind an API key. It holds an argon2 hash
of random bytes nobody retains, so no password can match it today — but that is
an accident of how it was created, not a rule, and ``users.set_password`` is one
admin click away from making it usable. This fragment is the rule.
"""
from __future__ import annotations


def login_eligible_sql(*, user: str) -> str:
    """Return a parameter-free SQL boolean over one ``api_users`` alias.

    ``user`` may be a table alias or the bare table name, for the two call sites
    that do not alias it.

    Wrapped in its own parentheses so it survives being spliced after an ``OR``,
    where an unwrapped ``A AND B`` would regroup and silently widen what the
    caller admits.
    """
    return f"({user}.disabled_at IS NULL AND {user}.is_service IS FALSE)"
```

In `src/localmail/api/auth.py`, add the import beside the existing `revocation_sql` one:

```python
from localmail.api.login_eligible_sql import login_eligible_sql
```

and in `login`, replace the SELECT:

```python
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, password_hash FROM api_users "
            "WHERE username = %s AND " + login_eligible_sql(user="api_users"),
            (username,),
        )
        row = cur.fetchone()
```

In `src/localmail/api/admin/auth.py`, add the same import and replace `authenticate_admin`'s SELECT:

```python
        cur.execute(
            "SELECT id, password_hash, is_admin FROM api_users"
            " WHERE username = %s AND " + login_eligible_sql(user="api_users"),
            (username,),
        )
```

In `src/localmail/serve/oauth/consent_router.py`, add the import and replace the inline SELECT:

```python
                cur.execute(
                    "SELECT id, password_hash FROM api_users "
                    "WHERE username = %s AND " + login_eligible_sql(user="api_users"),
                    (decision.username,),
                )
```

In `src/localmail/api/admin/users.py`, add a shared guard and call it from both mutators. Put the helper directly above `set_password`:

```python
def _reject_service_row(conn: psycopg.Connection, user_id: int, action: str) -> None:
    """A service user is an API key's principal; it must never gain a password
    or admin rights. Both are one admin click from turning a machine credential
    into an interactive one."""
    with conn.cursor() as cur:
        cur.execute("SELECT is_service FROM api_users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user with id={user_id}")
    if bool(row[0]):
        raise UserFieldError(f"cannot {action} an API-key principal")
```

Then as the first statement of `set_password`:

```python
    _reject_service_row(conn, user_id, "set a password on")
```

and in `set_admin`, inside the `is_admin` branch, before the existence check:

```python
    if not is_admin:
        _guard_not_last_admin(conn, user_id, "demote")
    else:
        _reject_service_row(conn, user_id, "promote")
        _user_state(conn, user_id)  # existence check → UserNotFound
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_service_user_cannot_log_in.py -q`
Expected: 7 passed.

Run: `unset VIRTUAL_ENV && uv run pytest -q`
Expected: full suite green — the fragment reproduces the previous `disabled_at IS NULL` behaviour for every non-service user.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/login_eligible_sql.py src/localmail/api/auth.py \
        src/localmail/api/admin/auth.py src/localmail/serve/oauth/consent_router.py \
        src/localmail/api/admin/users.py tests/test_service_user_cannot_log_in.py
git commit -m "feat(auth): one rule for login eligibility, applied at all three sites"
```

---

### Task 4: Rule 1 — a key never reaches an admin route

**Files:**
- Modify: `src/localmail/serve/admin/dependencies.py` (`require_admin`)
- Test: `tests/test_api_key_admin_bar.py`

**Interfaces:**
- Consumes: `AuthenticatedUser.is_api_key` from Task 2.
- Produces: nothing new; `require_admin()` raises `HTTPException(403)` for an API-key bearer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_key_admin_bar.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""An API key is refused at every admin route, even when its principal is an
admin. A bot key must never be able to mint another bot key."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.auth import hash_password, hash_token, issue_token
from localmail.config import ServeConfig
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def app(db_dsn):
    cfg = ServeConfig(
        session_signing_key=_SIGNING_KEY, state_signing_key="y" * 43,
        cookie_secure=False,
    )
    return create_app(db_dsn=db_dsn, serve_config=cfg)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


def _admin_key(db_conn: psycopg.Connection) -> str:
    """A service user promoted to admin by direct SQL.

    users.set_admin refuses this through the UI (Task 3) — which is the point:
    the gate must hold for a state the UI will not produce today, but a
    migration, a repair script, or a relaxed toggle could.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_service, is_admin) "
            "VALUES ('bot', 'x', TRUE, TRUE) RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        uid = int(row[0])
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, 'bot')",
            (hash_token("lmk_raw"), uid),
        )
    db_conn.commit()
    return "lmk_raw"


def test_an_admin_principals_api_key_is_still_refused(client, db_conn):
    key = _admin_key(db_conn)
    resp = client.get("/v1/admin/users", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 403


def test_a_real_admin_bearer_still_passes(client, db_conn):
    """Positive control: the guard must not close the native-client path."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE) RETURNING id",
            (hash_password("pw"),),
        )
        row = cur.fetchone()
    assert row is not None
    tok, _ = issue_token(db_conn, int(row[0]))
    db_conn.commit()
    resp = client.get("/v1/admin/users", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_key_admin_bar.py -q`
Expected: `test_an_admin_principals_api_key_is_still_refused` FAILS with `assert 200 == 403` — the key currently authenticates as an admin.

- [ ] **Step 3: Write the implementation**

In `src/localmail/serve/admin/dependencies.py`, inside `require_admin()`'s bearer branch, insert the check **before** the `is_admin` test:

```python
            if user is None:
                raise InvalidToken("token is invalid, expired, or revoked")
            if user.is_api_key:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="API keys cannot access admin routes",
                )
            if not user.is_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="not an admin"
                )
```

Extend the module docstring's `require_admin()` paragraph:

```python
require_admin() additionally accepts ``Authorization: Bearer <token>`` for
native clients — an admin bearer token is authorized with no CSRF (a bearer
header carries no ambient cookie credential, so CSRF does not apply); a
non-admin bearer is 403; a bad/expired bearer is 401. An **API key** is 403
regardless of its principal's is_admin flag: the check sits at the point of use
rather than at mint time because a service user can be promoted after its key
was minted. With no bearer header it falls back to the cookie path unchanged.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_key_admin_bar.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/dependencies.py tests/test_api_key_admin_bar.py
git commit -m "feat(admin): refuse API keys at the admin bearer gate"
```

---

### Task 5: Service layer — mint, list, revoke, delete

**Files:**
- Create: `src/localmail/api/admin/api_key_names.py`, `src/localmail/api/admin/api_keys.py`
- Test: `tests/test_api_admin_api_keys.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:
  - `api_key_name_error(name: str) -> str | None`
  - `API_KEY_PREFIX: str` (`"lmk_"`)
  - `ApiKeyFieldError(ValueError)`, `ApiKeyNotFound(Exception)`
  - `CreatedKey(user_id: int, name: str, raw_key: str)`
  - `ApiKeySummary(user_id: int, name: str, has_key: bool, key_created_at: datetime | None, last_used_at: datetime | None, disabled: bool, account_names: list[str])`
  - `create_key(conn, *, name: str, account_ids: list[int]) -> CreatedKey`
  - `list_keys(conn) -> list[ApiKeySummary]`
  - `set_grant(conn, user_id: int, account_id: int, granted: bool) -> None`
  - `revoke_key(conn, user_id: int) -> None`
  - `delete_key_principal(conn, user_id: int) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_admin_api_keys.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Service layer for admin-issued API keys."""
from __future__ import annotations

import psycopg
import pytest

from localmail.api.admin import api_keys as svc
from localmail.api.admin.api_key_names import api_key_name_error
from localmail.api.auth import hash_password, verify_token


def _account(conn: psycopg.Connection, name: str) -> int:
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


def test_name_validation_is_pure_and_message_shaped():
    assert api_key_name_error("bot") is None
    assert api_key_name_error("") == "name must not be blank"
    assert api_key_name_error("   ") == "name must not be blank"
    assert "longer than" in (api_key_name_error("x" * 129) or "")


def test_create_key_mints_a_working_credential(db_conn):
    aid = _account(db_conn, "work")
    created = svc.create_key(db_conn, name="my_mail_bot", account_ids=[aid])
    db_conn.commit()
    assert created.raw_key.startswith(svc.API_KEY_PREFIX)
    user = verify_token(db_conn, created.raw_key)
    assert user is not None
    assert user.id == created.user_id
    assert user.is_api_key is True


def test_the_principal_is_a_service_user_and_never_admin(db_conn):
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT username, is_service, is_admin FROM api_users WHERE id = %s",
            (created.user_id,),
        )
        row = cur.fetchone()
    assert row == ("bot", True, False)


def test_grants_are_applied(db_conn):
    aid = _account(db_conn, "work")
    created = svc.create_key(db_conn, name="bot", account_ids=[aid])
    db_conn.commit()
    rows = svc.list_keys(db_conn)
    assert [r.account_names for r in rows] == [["work"]]


def test_a_human_username_is_refused(db_conn):
    """Rule 1's front door: minting a key named after a human admin would hand
    out an admin credential."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE)",
            (hash_password("pw"),),
        )
    db_conn.commit()
    with pytest.raises(svc.ApiKeyFieldError):
        svc.create_key(db_conn, name="root", account_ids=[])
    db_conn.rollback()


def test_minting_twice_for_a_live_bot_is_refused(db_conn):
    svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with pytest.raises(svc.ApiKeyFieldError) as exc:
        svc.create_key(db_conn, name="bot", account_ids=[])
    assert "revoke" in str(exc.value)
    db_conn.rollback()


def test_revoke_keeps_the_principal_and_re_keying_keeps_the_grants(db_conn):
    """The whole reason revoke and delete are separate operations."""
    aid = _account(db_conn, "work")
    first = svc.create_key(db_conn, name="bot", account_ids=[aid])
    db_conn.commit()
    svc.revoke_key(db_conn, first.user_id)
    db_conn.commit()
    assert verify_token(db_conn, first.raw_key) is None

    rows = svc.list_keys(db_conn)
    assert len(rows) == 1
    assert rows[0].has_key is False
    assert rows[0].account_names == ["work"]

    second = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    assert second.user_id == first.user_id
    assert second.raw_key != first.raw_key
    assert svc.list_keys(db_conn)[0].account_names == ["work"]


def test_delete_principal_removes_the_bot_and_its_grants(db_conn):
    aid = _account(db_conn, "work")
    created = svc.create_key(db_conn, name="bot", account_ids=[aid])
    db_conn.commit()
    svc.delete_key_principal(db_conn, created.user_id)
    db_conn.commit()
    assert svc.list_keys(db_conn) == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM user_accounts")
        row = cur.fetchone()
    assert row is not None and row[0] == 0


def test_delete_principal_refuses_a_human_user(db_conn):
    """The route addresses principals by id; it must not become a way to delete
    a person."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES ('root', 'x') "
            "RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    with pytest.raises(svc.ApiKeyNotFound):
        svc.delete_key_principal(db_conn, int(row[0]))


def test_revoke_unknown_is_not_found(db_conn):
    with pytest.raises(svc.ApiKeyNotFound):
        svc.revoke_key(db_conn, 999999)


def test_an_unknown_account_is_a_field_error(db_conn):
    with pytest.raises(svc.ApiKeyFieldError):
        svc.create_key(db_conn, name="bot", account_ids=[999999])
    db_conn.rollback()


def test_list_never_carries_the_raw_key(db_conn):
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    rendered = repr(svc.list_keys(db_conn))
    assert created.raw_key not in rendered


def test_the_raw_key_is_stored_nowhere(db_conn):
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT token_sha256, api_key_name FROM api_tokens")
        rows = cur.fetchall()
    assert created.raw_key.encode() not in bytes(rows[0][0])
    assert rows[0][1] == "bot"


def test_set_grant_refuses_a_human_principal(db_conn):
    """Otherwise this is a second, unguarded way to edit a person's ACL."""
    aid = _account(db_conn, "work")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES ('root', 'x') "
            "RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    with pytest.raises(svc.ApiKeyNotFound):
        svc.set_grant(db_conn, int(row[0]), aid, True)


def test_a_key_reads_only_its_granted_accounts(db_conn):
    """Reach: the ACL applies to a key exactly as to any other credential."""
    from localmail.api.acl import allowed_account_ids

    granted = _account(db_conn, "work")
    _account(db_conn, "personal")
    created = svc.create_key(db_conn, name="bot", account_ids=[granted])
    db_conn.commit()
    assert allowed_account_ids(db_conn, created.user_id) == [granted]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_api_keys.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.api.admin.api_keys'`.

- [ ] **Step 3: Write the implementation**

Create `src/localmail/api/admin/api_key_names.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""API-key name rules. Pure: no IO, no DB.

The key's name is its principal's ``api_users.username``, so uniqueness comes
free from that column's constraint and is not re-stated here. Shaped like
``account_names.account_name_error`` — a message, or None — so each caller wraps
it in its own error type and renders it beside the offending field.
"""
from __future__ import annotations

#: Upper bound on an API-key name, in characters.
NAME_MAX_CHARS = 128


def api_key_name_error(name: str) -> str | None:
    """Return why ``name`` is unusable as an API-key name, or None if it is fine."""
    if not name or not name.strip():
        return "name must not be blank"
    if len(name.strip()) > NAME_MAX_CHARS:
        return f"name longer than {NAME_MAX_CHARS} chars"
    return None
```

Create `src/localmail/api/admin/api_keys.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Service layer for admin-issued API keys.

Transport-free: pure functions over a psycopg connection, no FastAPI imports.

A key is an ``api_tokens`` row with ``api_key_name`` set and ``expires_at NULL``,
minted against a dedicated **service user**. That principal is an ordinary
``api_users`` row, which is what lets the per-account ACL, ``disabled_at``, and
``sessions_invalidated_at`` reach the key with no code of their own here.

The pairing is 1:1 — one key per service user — enforced by migration 0036's
partial unique index. Everything therefore addresses a key by its principal's
id: ``api_tokens``' primary key is ``token_sha256``, which is credential
material and must never travel in a URL or a log line.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

from localmail.api import acl
from localmail.api.admin.api_key_names import api_key_name_error
from localmail.api.auth import generate_token, hash_password, hash_token

#: Marks a raw key as one, in logs and to secret scanners. Never consulted
#: during verification — there remains exactly one lookup path.
API_KEY_PREFIX = "lmk_"


class ApiKeyFieldError(ValueError):
    """Validation rejected a create (blank name, collision, unknown account)."""


class ApiKeyNotFound(Exception):
    """No API key, or no API-key principal, with that id."""


@dataclass(frozen=True)
class CreatedKey:
    user_id: int
    name: str
    raw_key: str


@dataclass(frozen=True)
class ApiKeySummary:
    user_id: int
    name: str
    has_key: bool
    key_created_at: datetime | None
    last_used_at: datetime | None
    disabled: bool
    account_names: list[str]


def list_keys(conn: psycopg.Connection) -> list[ApiKeySummary]:
    """Every API-key principal, with its key if it currently holds one.

    Driven from ``api_users``, not from ``api_tokens``: a bot whose key was
    revoked holds no token row, and it must stay visible so an operator can
    re-key or delete it.
    """
    with conn.cursor(row_factory=class_row(ApiKeySummary)) as cur:
        cur.execute(
            "SELECT u.id AS user_id, u.username AS name, "
            "       (t.api_key_name IS NOT NULL) AS has_key, "
            "       t.created_at AS key_created_at, "
            "       t.last_used_at AS last_used_at, "
            "       (u.disabled_at IS NOT NULL) AS disabled, "
            "       COALESCE("
            "         array_agg(a.name ORDER BY a.name) "
            "           FILTER (WHERE a.name IS NOT NULL), "
            "         '{}'"
            "       ) AS account_names "
            "  FROM api_users u "
            "  LEFT JOIN api_tokens t "
            "    ON t.user_id = u.id AND t.api_key_name IS NOT NULL "
            "  LEFT JOIN user_accounts ua ON ua.user_id = u.id "
            "  LEFT JOIN accounts a ON a.id = ua.account_id "
            " WHERE u.is_service IS TRUE "
            " GROUP BY u.id, u.username, t.api_key_name, t.created_at, "
            "          t.last_used_at, u.disabled_at "
            " ORDER BY u.username"
        )
        return cur.fetchall()


def _create_service_user(conn: psycopg.Connection, name: str) -> int:
    """Insert the principal with a password hash of random bytes nobody retains.

    Rule 2 (``login_eligible_sql``) is what makes it unusable; this only makes
    the NOT NULL column satisfiable.
    """
    unusable = hash_password(secrets.token_urlsafe(32))
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO api_users (username, password_hash, is_service) "
                "VALUES (%s, %s, TRUE) RETURNING id",
                (name, unusable),
            )
        except psycopg.errors.UniqueViolation as e:
            raise ApiKeyFieldError(f"name {name!r} is already taken") from e
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _resolve_principal(conn: psycopg.Connection, name: str) -> int:
    """Return the principal to mint against; create one if the name is free.

    Three outcomes, and the middle one is the re-key path: a service user
    holding no key is reused with its grants intact.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, is_service, "
            "       EXISTS (SELECT 1 FROM api_tokens t "
            "                WHERE t.user_id = api_users.id "
            "                  AND t.api_key_name IS NOT NULL) "
            "  FROM api_users WHERE username = %s",
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        return _create_service_user(conn, name)
    user_id, is_service, has_key = int(row[0]), bool(row[1]), bool(row[2])
    if not is_service:
        raise ApiKeyFieldError(
            f"{name!r} is an existing user account, not an API key"
        )
    if has_key:
        raise ApiKeyFieldError(
            f"API key {name!r} already exists; revoke it before minting a new one"
        )
    return user_id


def create_key(
    conn: psycopg.Connection, *, name: str, account_ids: list[int],
) -> CreatedKey:
    """Mint an API key and return the raw value — the only time it exists.

    Caller commits, and must run the whole call in one transaction: a failure
    after the principal is created would otherwise leave a row that the
    operator's retry then collides with.
    """
    err = api_key_name_error(name)
    if err is not None:
        raise ApiKeyFieldError(err)
    name = name.strip()
    user_id = _resolve_principal(conn, name)
    for account_id in account_ids:
        try:
            acl.grant_account(conn, user_id, account_id)
        except psycopg.errors.ForeignKeyViolation as e:
            raise ApiKeyFieldError(f"unknown account {account_id}") from e
    raw_key = API_KEY_PREFIX + generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, %s)",
            (hash_token(raw_key), user_id, name),
        )
    return CreatedKey(user_id=user_id, name=name, raw_key=raw_key)


def set_grant(
    conn: psycopg.Connection, user_id: int, account_id: int, granted: bool,
) -> None:
    """Grant or revoke one account on an API-key principal. Caller commits.

    The ``is_service`` check is what keeps this from becoming a second,
    unguarded way to edit a *person's* ACL — that belongs to
    ``users.set_grant``, which has its own guards.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM api_users WHERE id = %s AND is_service IS TRUE",
            (user_id,),
        )
        if cur.fetchone() is None:
            raise ApiKeyNotFound(f"no API-key principal with id={user_id}")
    if granted:
        try:
            acl.grant_account(conn, user_id, account_id)
        except psycopg.errors.ForeignKeyViolation as e:
            raise ApiKeyFieldError(f"unknown account {account_id}") from e
    else:
        acl.revoke_account(conn, user_id, account_id)


def revoke_key(conn: psycopg.Connection, user_id: int) -> None:
    """Delete the credential, keeping the principal and its grants. Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_tokens "
            " WHERE user_id = %s AND api_key_name IS NOT NULL",
            (user_id,),
        )
        if cur.rowcount == 0:
            raise ApiKeyNotFound(f"no API key for principal id={user_id}")


def delete_key_principal(conn: psycopg.Connection, user_id: int) -> None:
    """Delete the bot entirely; token and grants cascade. Caller commits.

    The ``is_service`` predicate is load-bearing: this is addressed by user id,
    and without it the route becomes a second way to delete a person.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_users WHERE id = %s AND is_service IS TRUE",
            (user_id,),
        )
        if cur.rowcount == 0:
            raise ApiKeyNotFound(f"no API-key principal with id={user_id}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_admin_api_keys.py -q`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/api_key_names.py src/localmail/api/admin/api_keys.py \
        tests/test_api_admin_api_keys.py
git commit -m "feat(admin): API-key service layer with 1:1 service principals"
```

---

### Task 6: JSON routes — `/v1/admin/api-keys`

**Files:**
- Create: `src/localmail/serve/admin/api_keys_router.py`
- Modify: `src/localmail/serve/app.py`
- Test: `tests/test_serve_admin_api_keys.py`

**Interfaces:**
- Consumes: Task 5's service layer; `require_admin` (Task 4); `check_csrf`; `parse_int_id`.
- Produces: `router` (an `APIRouter`), mounted at prefix `/v1/admin`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serve_admin_api_keys.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""JSON routes for /v1/admin/api-keys, driven by an admin bearer token."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.auth import hash_password, hash_token, issue_token
from localmail.config import ServeConfig
from localmail.serve.app import create_app


@pytest.fixture
def app(db_dsn):
    cfg = ServeConfig(
        session_signing_key="x" * 43, state_signing_key="y" * 43, cookie_secure=False,
    )
    return create_app(db_dsn=db_dsn, serve_config=cfg)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def admin_headers(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE) RETURNING id",
            (hash_password("pw"),),
        )
        row = cur.fetchone()
    assert row is not None
    tok, _ = issue_token(db_conn, int(row[0]))
    db_conn.commit()
    return {"Authorization": f"Bearer {tok}"}


def _account(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, config) "
            "VALUES (%s, %s, 'password', 'imap.example', 993, '{}'::jsonb) RETURNING id",
            (name, f"{name}@b.test"),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_create_returns_the_key_once(client, db_conn, admin_headers):
    aid = _account(db_conn, "work")
    resp = client.post(
        "/v1/admin/api-keys",
        json={"name": "my_mail_bot", "account_ids": [str(aid)]},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["api_key"].startswith("lmk_")
    assert body["name"] == "my_mail_bot"
    assert isinstance(body["id"], str)

    listed = client.get("/v1/admin/api-keys", headers=admin_headers).json()
    assert "api_key" not in listed["api_keys"][0]
    assert body["api_key"] not in str(listed)


def test_list_reports_grants_and_key_presence(client, db_conn, admin_headers):
    aid = _account(db_conn, "work")
    client.post(
        "/v1/admin/api-keys",
        json={"name": "bot", "account_ids": [str(aid)]},
        headers=admin_headers,
    )
    row = client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"][0]
    assert row["name"] == "bot"
    assert row["has_key"] is True
    assert row["account_names"] == ["work"]


def test_revoke_keeps_the_principal(client, db_conn, admin_headers):
    created = client.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers=admin_headers,
    ).json()
    resp = client.delete(f"/v1/admin/api-keys/{created['id']}", headers=admin_headers)
    assert resp.status_code == 204
    row = client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"][0]
    assert row["has_key"] is False


def test_delete_principal_removes_the_row(client, db_conn, admin_headers):
    created = client.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers=admin_headers,
    ).json()
    resp = client.delete(
        f"/v1/admin/api-keys/{created['id']}/principal", headers=admin_headers
    )
    assert resp.status_code == 204
    assert client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"] == []


def test_a_duplicate_name_is_400(client, db_conn, admin_headers):
    client.post("/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
                headers=admin_headers)
    resp = client.post("/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
                       headers=admin_headers)
    assert resp.status_code == 400


def test_an_unknown_id_is_404(client, db_conn, admin_headers):
    assert client.delete("/v1/admin/api-keys/999999", headers=admin_headers).status_code == 404


def test_a_non_digit_id_is_400(client, db_conn, admin_headers):
    assert client.delete("/v1/admin/api-keys/abc", headers=admin_headers).status_code == 400


def test_grants_can_be_edited(client, db_conn, admin_headers):
    aid = _account(db_conn, "work")
    created = client.post("/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
                          headers=admin_headers).json()
    resp = client.post(
        f"/v1/admin/api-keys/{created['id']}/grants",
        json={"account_id": str(aid), "granted": True},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    row = client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"][0]
    assert row["account_names"] == ["work"]


def test_an_api_key_cannot_mint_another(client, db_conn, admin_headers):
    """Rule 1 end-to-end on the route that matters most."""
    created = client.post("/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
                          headers=admin_headers).json()
    resp = client.post(
        "/v1/admin/api-keys", json={"name": "bot2", "account_ids": []},
        headers={"Authorization": f"Bearer {created['api_key']}"},
    )
    assert resp.status_code == 403


def test_a_key_reads_only_its_granted_accounts_over_http(client, db_conn, admin_headers):
    """Reach, end to end: the key drives a real /v1 read through the middleware
    and sees exactly its grants."""
    granted = _account(db_conn, "work")
    _account(db_conn, "personal")
    created = client.post(
        "/v1/admin/api-keys",
        json={"name": "bot", "account_ids": [str(granted)]},
        headers=admin_headers,
    ).json()
    resp = client.get(
        "/v1/accounts", headers={"Authorization": f"Bearer {created['api_key']}"}
    )
    assert resp.status_code == 200
    assert [a["name"] for a in resp.json()] == ["work"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_api_keys.py -q`
Expected: FAIL — every request 404s; the router is not mounted.

- [ ] **Step 3: Write the implementation**

Create `src/localmail/serve/admin/api_keys_router.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""HTTP routes for /v1/admin/api-keys.

Thin wrapper over `localmail.api.admin.api_keys`. Every route requires an admin
credential; every mutating route validates a method-bound CSRF token. IDs are
strings on the wire (#33), and the id of an API key is its principal's id.

Guard mapping: validation (`ApiKeyFieldError`) → 400; absence
(`ApiKeyNotFound`) → 404.

The raw key appears in exactly one response — the 201 from create — and in no
other route, because nothing can recover it afterwards.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel

from localmail.api.admin import api_keys as svc
from localmail.api.admin.auth import AdminUser
from localmail.api.ids import parse_int_id
from localmail.serve.admin.csrf import check_csrf
from localmail.serve.admin.dependencies import require_admin

router = APIRouter(tags=["admin-api-keys"])


class _KeyIn(BaseModel):
    name: str
    account_ids: list[str] = []


class _GrantIn(BaseModel):
    account_id: str
    granted: bool


def _summary_dict(k: svc.ApiKeySummary) -> dict:
    return {
        "id": str(k.user_id),
        "name": k.name,
        "has_key": k.has_key,
        "key_created_at": k.key_created_at.isoformat() if k.key_created_at else None,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "disabled": k.disabled,
        "account_names": k.account_names,
    }


@router.get("/api-keys")
def list_api_keys(request: Request, admin: AdminUser = require_admin()) -> dict:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_keys(conn)
    return {"api_keys": [_summary_dict(r) for r in rows]}


@router.post("/api-keys", status_code=201)
def create_api_key(
    body: _KeyIn, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    check_csrf(request, admin, x_csrf_token, "/v1/admin/api-keys")
    account_ids = [parse_int_id(a, field="account_id") for a in body.account_ids]
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            created = svc.create_key(
                conn, name=body.name, account_ids=account_ids
            )
        except svc.ApiKeyFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": str(created.user_id),
        "name": created.name,
        "api_key": created.raw_key,
    }


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: str, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/api-keys/{key_id}")
    uid = parse_int_id(key_id, field="key_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.revoke_key(conn, uid)
        except svc.ApiKeyNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
    return Response(status_code=204)


@router.delete("/api-keys/{key_id}/principal", status_code=204)
def delete_api_key_principal(
    key_id: str, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(
        request, admin, x_csrf_token, f"/v1/admin/api-keys/{key_id}/principal"
    )
    uid = parse_int_id(key_id, field="key_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_key_principal(conn, uid)
        except svc.ApiKeyNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
    return Response(status_code=204)


@router.post("/api-keys/{key_id}/grants")
def set_api_key_grant(
    key_id: str, body: _GrantIn, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/api-keys/{key_id}/grants")
    uid = parse_int_id(key_id, field="key_id")
    account_id = parse_int_id(body.account_id, field="account_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.set_grant(conn, uid, account_id, body.granted)
        except svc.ApiKeyNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except svc.ApiKeyFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        conn.commit()
    return {"ok": True}
```

In `src/localmail/serve/app.py`, add the import beside the other admin routers:

```python
from localmail.serve.admin import api_keys_router as admin_api_keys_router
```

and mount it beside `admin_users_router`:

```python
        app.include_router(admin_api_keys_router.router, prefix="/v1/admin")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_api_keys.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/api_keys_router.py src/localmail/serve/app.py \
        tests/test_serve_admin_api_keys.py
git commit -m "feat(serve): JSON admin routes for API keys"
```

---

### Task 7: Admin panel — `/admin/api-keys`

**Files:**
- Create: `src/localmail/serve/admin/api_key_forms.py`, `src/localmail/serve/admin/api_keys_panel_router.py`, `src/localmail/serve/admin/templates/api_keys/{list,_table,_row,_created}.html`, `src/localmail/serve/admin/static/api-keys-panel.js`
- Modify: `src/localmail/serve/admin/templates/base.html`, `src/localmail/serve/admin/templates/dashboard.html`, `src/localmail/serve/app.py`
- Test: `tests/test_serve_admin_api_key_screens.py`

**Interfaces:**
- Consumes: Task 5's service layer; `require_admin_session`; `csrf_token_context`.
- Produces: `form_to_create_kwargs(name: object, account_ids: list[str]) -> dict`, `field_errors_from(err) -> dict[str, str]`, `FormError(ValueError)`; `router` mounted at prefix `/admin`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serve_admin_api_key_screens.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""HTMX admin screens for API keys."""
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
def app(db_dsn):
    cfg = ServeConfig(
        session_signing_key=_SIGNING_KEY, state_signing_key="y" * 43,
        cookie_secure=False,
    )
    return create_app(db_dsn=db_dsn, serve_config=cfg)


@pytest.fixture
def admin_id(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE) RETURNING id",
            (hash_password("pw"),),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    return int(row[0])


@pytest.fixture
def client(app, admin_id):
    """Cookie-authenticated admin, mirroring tests/test_serve_admin_bearer_auth.py:
    the login form's own CSRF token must be scraped and posted back."""
    c = TestClient(app, follow_redirects=False)
    form = c.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    resp = c.post(
        "/admin/login",
        data={"username": "root", "password": "pw", "csrf_token": m.group(1)},
    )
    assert resp.status_code == 303, resp.text
    return c


def _csrf(admin_id: int, method: str, path: str) -> str:
    return make_csrf_token(
        user_id=admin_id, action=csrf_action(method, path),
        key=_SIGNING_KEY.encode("ascii"),
    )


def _account(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, config) "
            "VALUES (%s, %s, 'password', 'imap.example', 993, '{}'::jsonb) RETURNING id",
            (name, f"{name}@b.test"),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_list_screen_renders(client):
    resp = client.get("/admin/api-keys")
    assert resp.status_code == 200
    assert "API keys" in resp.text


def test_nav_links_to_the_panel(client):
    assert 'href="/admin/api-keys"' in client.get("/admin/").text


def test_create_shows_the_key_exactly_once(client, db_conn, admin_id):
    aid = _account(db_conn, "work")
    resp = client.post(
        "/admin/api-keys",
        data={"name": "my_mail_bot", "account_ids": [str(aid)]},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    assert resp.status_code == 200
    keys = re.findall(r"lmk_[A-Za-z0-9_\-]+", resp.text)
    assert len(keys) >= 1
    listed = client.get("/admin/api-keys").text
    assert keys[0] not in listed


def test_create_rejects_a_blank_name_inline(client, admin_id):
    resp = client.post(
        "/admin/api-keys",
        data={"name": "  "},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    assert resp.status_code == 400
    assert "blank" in resp.text


def test_create_without_csrf_is_400(client):
    resp = client.post("/admin/api-keys", data={"name": "bot"})
    assert resp.status_code == 400


def test_revoke_from_the_panel(client, admin_id):
    client.post(
        "/admin/api-keys", data={"name": "bot"},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    listed = client.get("/admin/api-keys").text
    assert "bot" in listed
    uid_match = re.search(r'id="api-key-row-(\d+)"', listed)
    assert uid_match
    uid = uid_match.group(1)
    resp = client.post(
        f"/admin/api-keys/{uid}/revoke",
        headers={
            "X-CSRF-Token": _csrf(admin_id, "POST", f"/admin/api-keys/{uid}/revoke")
        },
    )
    assert resp.status_code == 200
    assert "no key" in client.get("/admin/api-keys").text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_api_key_screens.py -q`
Expected: FAIL — `/admin/api-keys` 404s.

- [ ] **Step 3: Write the implementation**

Create `src/localmail/serve/admin/api_key_forms.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure form-parsing helpers for the API-key admin screen (no IO)."""
from __future__ import annotations

from localmail.api.admin.api_keys import ApiKeyFieldError


class FormError(ValueError):
    """Malformed raw form input the service layer wouldn't otherwise see."""


def form_to_create_kwargs(name: object, account_ids: list[str]) -> dict:
    """Map the raw create-form values to create_key(**kwargs)."""
    cleaned = str(name or "").strip()
    if not cleaned:
        raise FormError("name must not be blank")
    parsed: list[int] = []
    for raw in account_ids:
        if not str(raw).isdigit():
            raise FormError(f"malformed account id {raw!r}")
        parsed.append(int(raw))
    return {"name": cleaned, "account_ids": parsed}


def field_errors_from(err: ApiKeyFieldError | FormError) -> dict[str, str]:
    """Map a validation error to {field: message}; fall back to '_form'."""
    msg = str(err)
    if "name" in msg:
        return {"name": msg}
    return {"_form": msg}
```

Create `src/localmail/serve/admin/api_keys_panel_router.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Admin API-key HTML screens.

Thin server-rendered HTMX router mounted at /admin. One screen: name the
consumer, tick the accounts it may read, receive the key once. JSON machine
clients use /v1/admin/api-keys.

The raw key is rendered by exactly one fragment — the create response — because
it is stored as a SHA-256 and cannot be recovered afterwards.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
from fastapi import APIRouter, Header, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from localmail.api.admin import api_keys as svc
from localmail.api.admin.auth import AdminUser
from localmail.api.ids import parse_int_id
from localmail.serve.admin import api_key_forms as forms
from localmail.serve.admin.csrf import check_csrf, csrf_token_context, session_signing_key
from localmail.serve.admin.dependencies import require_admin_session

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


def _base_context(request: Request, admin: AdminUser) -> dict:
    return {
        "current_user": admin,
        "flashes": [],
        **csrf_token_context(user_id=admin.id, key=session_signing_key(request)),
    }


def _accounts(conn: psycopg.Connection) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM accounts ORDER BY name")
        return [(int(i), n) for i, n in cur.fetchall()]


def _list_context(request: Request, admin: AdminUser) -> dict:
    pool = request.app.state.pool
    with pool.connection() as conn:
        keys = svc.list_keys(conn)
        accounts = _accounts(conn)
    ctx = _base_context(request, admin)
    ctx.update({"keys": keys, "accounts": accounts, "field_errors": {}, "created": None})
    return ctx


@router.get("/api-keys", response_class=HTMLResponse)
def list_api_keys(
    request: Request, admin: AdminUser = require_admin_session(),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="api_keys/list.html",
        context=_list_context(request, admin),
    )


@router.post("/api-keys", response_class=HTMLResponse)
async def create_api_key(
    request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, "/admin/api-keys")
    form = await request.form()
    try:
        kwargs = forms.form_to_create_kwargs(
            form.get("name"), [str(v) for v in form.getlist("account_ids")]
        )
    except forms.FormError as e:
        ctx = _base_context(request, admin)
        ctx.update({"field_errors": forms.field_errors_from(e), "created": None})
        return templates.TemplateResponse(
            request=request, name="api_keys/_created.html", context=ctx,
            status_code=400,
        )
    pool = request.app.state.pool

    def _create() -> svc.CreatedKey:
        with pool.connection() as conn:
            created = svc.create_key(conn, **kwargs)
            conn.commit()
            return created

    try:
        created = await run_in_threadpool(_create)
    except svc.ApiKeyFieldError as e:
        ctx = _base_context(request, admin)
        ctx.update({"field_errors": forms.field_errors_from(e), "created": None})
        return templates.TemplateResponse(
            request=request, name="api_keys/_created.html", context=ctx,
            status_code=400,
        )
    ctx = _base_context(request, admin)
    ctx.update({"created": created, "field_errors": {}})
    return templates.TemplateResponse(
        request=request, name="api_keys/_created.html", context=ctx
    )


@router.post("/api-keys/{key_id}/revoke", response_class=HTMLResponse)
def revoke_api_key(
    key_id: str, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/api-keys/{key_id}/revoke")
    uid = parse_int_id(key_id, field="key_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.revoke_key(conn, uid)
            conn.commit()
        except svc.ApiKeyNotFound:
            conn.rollback()
    return templates.TemplateResponse(
        request=request, name="api_keys/_table.html",
        context=_list_context(request, admin),
    )


@router.post("/api-keys/{key_id}/delete", response_class=HTMLResponse)
def delete_api_key_principal(
    key_id: str, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/api-keys/{key_id}/delete")
    uid = parse_int_id(key_id, field="key_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_key_principal(conn, uid)
            conn.commit()
        except svc.ApiKeyNotFound:
            conn.rollback()
    return templates.TemplateResponse(
        request=request, name="api_keys/_table.html",
        context=_list_context(request, admin),
    )
```

Create `src/localmail/serve/admin/templates/api_keys/list.html`:

```html
{% extends "base.html" %}
{% block title %}API keys — localmail admin{% endblock %}
{% block content %}
<script src="/admin/static/api-keys-panel.js" defer></script>
<div class="admin-card">
  <h1>API keys</h1>
  <p>An API key lets another process read this archive. It is shown once, when
     you create it, and cannot be recovered afterwards.</p>
  <form id="api-key-form" class="account-form"
        hx-post="/admin/api-keys" hx-target="#api-key-created" hx-swap="innerHTML"
        hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/api-keys") }}"}'>
    <label for="api-key-name">Name</label>
    <input id="api-key-name" name="name" required placeholder="my_mail_bot">
    <fieldset>
      <legend>Accounts this key may read</legend>
      {% for account_id, account_name in accounts %}
        <label><input type="checkbox" name="account_ids" value="{{ account_id }}"> {{ account_name }}</label>
      {% else %}
        <p>No accounts configured yet.</p>
      {% endfor %}
    </fieldset>
    <button type="submit">Create key</button>
  </form>
  <div id="api-key-created"></div>
</div>
<div class="admin-card" id="api-key-table">
  {% include "api_keys/_table.html" %}
</div>
{% endblock %}
```

Create `src/localmail/serve/admin/templates/api_keys/_table.html`:

```html
<table class="accounts-table">
  <thead>
    <tr><th>Name</th><th>Key</th><th>Accounts</th><th>Last used</th><th></th></tr>
  </thead>
  <tbody>
    {% for k in keys %}
      {% include "api_keys/_row.html" %}
    {% else %}
    <tr><td colspan="5">No API keys.</td></tr>
    {% endfor %}
  </tbody>
</table>
```

Create `src/localmail/serve/admin/templates/api_keys/_row.html`:

```html
<tr id="api-key-row-{{ k.user_id }}">
  <td>{{ k.name }}</td>
  <td>{% if k.has_key %}<span class="sync-on">active</span>{% else %}<span class="sync-off">no key</span>{% endif %}</td>
  <td>{{ k.account_names | join(", ") or "—" }}</td>
  <td>{{ k.last_used_at.strftime("%Y-%m-%d %H:%M") if k.last_used_at else "never" }}</td>
  <td class="account-row-actions">
    {% if k.has_key %}
      <button type="button"
              hx-post="/admin/api-keys/{{ k.user_id }}/revoke"
              hx-target="#api-key-table" hx-swap="innerHTML"
              hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/api-keys/" ~ k.user_id ~ "/revoke") }}"}'>Revoke key</button>
    {% endif %}
    <button type="button"
            hx-post="/admin/api-keys/{{ k.user_id }}/delete"
            hx-confirm="Delete {{ k.name }} and its account grants?"
            hx-target="#api-key-table" hx-swap="innerHTML"
            hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/api-keys/" ~ k.user_id ~ "/delete") }}"}'>Delete</button>
  </td>
</tr>
```

Create `src/localmail/serve/admin/templates/api_keys/_created.html`:

```html
{% if field_errors %}
  <p class="field-error">{{ field_errors.values() | join(" ") }}</p>
{% elif created %}
  <div class="admin-flash admin-flash-success">
    <p><strong>Copy this key now.</strong> It is not stored and cannot be shown again.</p>
    <input id="api-key-value" readonly value="{{ created.raw_key }}" size="60">
    <button type="button" data-copy-target="api-key-value">Copy</button>
    <p>Give it to the consumer as
       <code>Authorization: Bearer {{ created.raw_key[:8] }}…</code></p>
  </div>
{% endif %}
```

Create `src/localmail/serve/admin/static/api-keys-panel.js`:

```javascript
// The /admin CSP is script-src 'self' with no unsafe-inline, so the copy
// button cannot be wired with an inline handler or an htmx hx-on:: attribute.
document.addEventListener("click", (ev) => {
  const button = ev.target.closest("[data-copy-target]");
  if (!button) return;
  const field = document.getElementById(button.dataset.copyTarget);
  if (!field) return;
  field.select();
  navigator.clipboard.writeText(field.value).then(() => {
    button.textContent = "Copied";
  });
});
```

In `src/localmail/serve/admin/templates/base.html`, add the nav link after Users:

```html
        <a href="/admin/users">Users</a>
        <a href="/admin/api-keys">API keys</a>
```

In `src/localmail/serve/admin/templates/dashboard.html`, add a card after the Users `<li>`:

```html
    <li><a href="/admin/api-keys"><strong>API keys</strong></a> — issue a key so
      another process can read this archive.</li>
```

In `src/localmail/serve/app.py`, import and mount the panel router beside `admin_users_panel_router`:

```python
from localmail.serve.admin import api_keys_panel_router as admin_api_keys_panel_router
```

```python
        app.include_router(admin_api_keys_panel_router.router, prefix="/admin")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_api_key_screens.py -q`
Expected: 6 passed.

Then confirm the CSP test still passes (the new script is a served file, not inline):

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_csp.py -q`
Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/api_key_forms.py \
        src/localmail/serve/admin/api_keys_panel_router.py \
        src/localmail/serve/admin/templates/api_keys \
        src/localmail/serve/admin/static/api-keys-panel.js \
        src/localmail/serve/admin/templates/base.html \
        src/localmail/serve/admin/templates/dashboard.html \
        src/localmail/serve/app.py \
        tests/test_serve_admin_api_key_screens.py
git commit -m "feat(admin): API-key panel — name in, key out, shown once"
```

---

### Task 8: CLI — `add-api-key`, `list-api-keys`, `revoke-api-key`, `remove-api-key`

**Files:**
- Modify: `src/localmail/cli.py` (add after `list-api-users`)
- Test: `tests/test_cli_api_keys.py`

**Interfaces:**
- Consumes: Task 5's service layer; `_dsn_from_ctx(ctx)` (the resolver its neighbours in that region of `cli.py` use).
- Produces: four click commands on `main`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_api_keys.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""CLI surface for API keys. stdout carries only the key, so a provisioning
script can capture it."""
from __future__ import annotations

import psycopg
import pytest
from click.testing import CliRunner

from localmail.cli import main


@pytest.fixture
def runner(db_dsn, monkeypatch):
    monkeypatch.setenv("LOCALMAIL_DSN_OVERRIDE", db_dsn)
    return CliRunner()


def _account(conn: psycopg.Connection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, config) "
            "VALUES (%s, %s, 'password', 'imap.example', 993, '{}'::jsonb)",
            (name, f"{name}@b.test"),
        )
    conn.commit()


def test_add_prints_only_the_key_on_stdout(runner, db_conn):
    _account(db_conn, "work")
    result = runner.invoke(
        main, ["add-api-key", "my_mail_bot", "--grant", "work"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert result.stdout.strip().startswith("lmk_")
    assert len(result.stdout.strip().splitlines()) == 1


def test_add_rejects_an_unknown_account(runner, db_conn):
    result = runner.invoke(main, ["add-api-key", "bot", "--grant", "nope"])
    assert result.exit_code != 0
    assert "no such account" in result.output


def test_add_rejects_a_duplicate(runner, db_conn):
    runner.invoke(main, ["add-api-key", "bot"], catch_exceptions=False)
    result = runner.invoke(main, ["add-api-key", "bot"])
    assert result.exit_code != 0


def test_list_shows_names_and_grants(runner, db_conn):
    _account(db_conn, "work")
    runner.invoke(main, ["add-api-key", "bot", "--grant", "work"],
                  catch_exceptions=False)
    result = runner.invoke(main, ["list-api-keys"], catch_exceptions=False)
    assert "bot" in result.output
    assert "work" in result.output


def test_revoke_keeps_the_principal_then_re_key_works(runner, db_conn):
    _account(db_conn, "work")
    runner.invoke(main, ["add-api-key", "bot", "--grant", "work"],
                  catch_exceptions=False)
    assert runner.invoke(main, ["revoke-api-key", "bot"]).exit_code == 0
    assert "no key" in runner.invoke(main, ["list-api-keys"]).output
    second = runner.invoke(main, ["add-api-key", "bot"], catch_exceptions=False)
    assert second.exit_code == 0
    assert "work" in runner.invoke(main, ["list-api-keys"]).output


def test_remove_deletes_the_principal(runner, db_conn):
    runner.invoke(main, ["add-api-key", "bot"], catch_exceptions=False)
    assert runner.invoke(main, ["remove-api-key", "bot"]).exit_code == 0
    assert "(no API keys)" in runner.invoke(main, ["list-api-keys"]).output


def test_revoke_unknown_is_an_error(runner, db_conn):
    assert runner.invoke(main, ["revoke-api-key", "ghost"]).exit_code != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_api_keys.py -q`
Expected: FAIL — `Error: No such command 'add-api-key'`.

- [ ] **Step 3: Write the implementation**

In `src/localmail/cli.py`, insert these four commands immediately after `list_api_users`:

```python
@main.command("add-api-key")
@click.argument("name")
@click.option("--grant", "grants", multiple=True, metavar="ACCOUNT",
              help="Account name this key may read; repeat for several.")
@click.pass_context
def add_api_key(ctx: click.Context, name: str, grants: tuple[str, ...]) -> None:
    """Create an API key for a machine consumer.

    The key is printed to stdout once and stored only as a SHA-256; it cannot
    be recovered afterwards. Everything else goes to stderr so a provisioning
    script can capture stdout verbatim.
    """
    from localmail.api.acl import resolve_account_id_by_name
    from localmail.api.admin import api_keys as svc
    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        account_ids = []
        for account_name in grants:
            account_id = resolve_account_id_by_name(conn, account_name)
            if account_id is None:
                raise click.ClickException(f"no such account: {account_name!r}")
            account_ids.append(account_id)
        try:
            created = svc.create_key(conn, name=name, account_ids=account_ids)
        except svc.ApiKeyFieldError as e:
            raise click.ClickException(str(e))
        conn.commit()
    click.echo(created.raw_key)
    click.echo(
        f"created API key {created.name!r} (id={created.user_id}). "
        f"It is shown once — store it now.",
        err=True,
    )
    if not grants:
        click.echo(
            f"note: no account grants yet. Use "
            f"`localmail grant-account {created.name} <account-name>` to give "
            f"this key read access to mail.",
            err=True,
        )


@main.command("list-api-keys")
@click.pass_context
def list_api_keys(ctx: click.Context) -> None:
    """List API keys, their granted accounts, and when each was last used."""
    from localmail.api.admin import api_keys as svc
    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        rows = svc.list_keys(conn)
    if not rows:
        click.echo("(no API keys)")
        return
    for row in rows:
        state = "active" if row.has_key else "no key"
        if row.disabled:
            state += ", disabled"
        last_used = row.last_used_at.strftime("%Y-%m-%d") if row.last_used_at else "never"
        click.echo(f"{row.name} [{state}] last-used={last_used}")
        click.echo(f"  accounts: {', '.join(row.account_names) or '(none)'}")


@main.command("revoke-api-key")
@click.argument("name")
@click.pass_context
def revoke_api_key(ctx: click.Context, name: str) -> None:
    """Revoke an API key, keeping its principal and account grants.

    Re-mint under the same name with `localmail add-api-key NAME`; the grants
    survive.
    """
    from localmail.api.acl import resolve_user_id_by_username
    from localmail.api.admin import api_keys as svc
    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        user_id = resolve_user_id_by_username(conn, name)
        if user_id is None:
            raise click.ClickException(f"no such API key: {name!r}")
        try:
            svc.revoke_key(conn, user_id)
        except svc.ApiKeyNotFound as e:
            raise click.ClickException(str(e))
        conn.commit()
    click.echo(f"revoked API key {name!r}")


@main.command("remove-api-key")
@click.argument("name")
@click.pass_context
def remove_api_key(ctx: click.Context, name: str) -> None:
    """Delete an API key's principal, its key, and its account grants."""
    from localmail.api.acl import resolve_user_id_by_username
    from localmail.api.admin import api_keys as svc
    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        user_id = resolve_user_id_by_username(conn, name)
        if user_id is None:
            raise click.ClickException(f"no such API key: {name!r}")
        try:
            svc.delete_key_principal(conn, user_id)
        except svc.ApiKeyNotFound as e:
            raise click.ClickException(str(e))
        conn.commit()
    click.echo(f"removed API key {name!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_api_keys.py -q`
Expected: 7 passed.

Then the config-path pin, which discovers new commands on its own:

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_config_path.py -q`
Expected: passed. If a new command fails it, it is reading the default config path instead of `_dsn_from_ctx(ctx)`.

Also re-run the whole suite, because several pins read the live `main.commands`
registry and so take the four new commands into scope automatically — the
version-diagnostic reach pin (#304) and the self-reporting skip-set pin among
them:

Run: `unset VIRTUAL_ENV && uv run pytest -q`
Expected: full suite green. A failure naming a new command means it is missing
the group callback's version report path, not that the command is wrong.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_api_keys.py
git commit -m "feat(cli): add/list/revoke/remove API keys"
```

---

### Task 9: Documentation

**Files:**
- Modify: `README.md`, `docs/mcp-usage.md`, `CLAUDE.md`
- Test: none (prose). Verified by the full suite still passing.

**Interfaces:**
- Consumes: everything above.
- Produces: nothing code-facing.

- [ ] **Step 1: Add the README onboarding section**

Find the section that documents API users (search for `add-api-user`). Add immediately after it:

````markdown
### Giving another process access (API keys)

An API key lets a bot, a cron job, or an AI agent read the archive without a
password and without a token that expires.

```bash
localmail add-api-key my_mail_bot --grant work --grant personal
# lmk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The key is printed once, to stdout, and stored only as a SHA-256 — it cannot be
recovered. Everything else the command says goes to stderr, so
`KEY=$(localmail add-api-key my_mail_bot --grant work)` captures exactly the key.

The consumer presents it as an ordinary bearer credential, which works for both
`/v1/*` and `/mcp`:

```bash
curl -H "Authorization: Bearer $KEY" https://localhost:8443/v1/messages
```

A key reads only the accounts it was granted, and **never** reaches an admin
route — a leaked key cannot mint another key or change account configuration.

To rotate: `localmail revoke-api-key my_mail_bot` then `localmail add-api-key
my_mail_bot`. The account grants survive, so there is nothing to re-tick; the
old key stops working the moment it is revoked.

Admins can do all of this at **/admin/api-keys** in the web UI instead.
````

- [ ] **Step 2: Update `docs/mcp-usage.md`**

Find the quick-start step that obtains a token from `POST /v1/auth/login`. Add above it:

````markdown
The simplest way to authenticate an agent is an API key, which an admin issues
and which never expires:

```bash
localmail add-api-key my_agent --grant work
```

Use the printed value as the bearer token below. A login-issued token still
works and is what you want for a human-driven client; an API key is for an
unattended one.
````

- [ ] **Step 3: Update `CLAUDE.md`**

In the **Commands** block, add beside the other API-user commands:

```
uv run localmail add-api-key N [--grant ACCOUNT]…  # mint a never-expiring key (stdout = the key)
uv run localmail list-api-keys                     # keys, grants, last-used
uv run localmail revoke-api-key N                  # kill the credential, keep the bot + grants
uv run localmail remove-api-key N                  # delete the bot entirely
```

In the **GUI server** section, after the session-revocation bullet list, add:

````markdown
- **API keys are a fifth credential kind, and deliberately not a fifth code
  path (migration `0036`).** A key is an `api_tokens` row with `api_key_name`
  set and `expires_at NULL`, minted against a dedicated **service user**
  (`api_users.is_service`). Because the principal is an ordinary `api_users`
  row, the per-account ACL, `disabled_at` and `sessions_invalidated_at` all
  reach it with no code of their own — which is the whole reason it is not its
  own table. Design:
  [docs/superpowers/specs/2026-08-24-admin-api-keys-design.md](docs/superpowers/specs/2026-08-24-admin-api-keys-design.md).
  - **The CHECK is the load-bearing half of the migration.** Dropping
    `NOT NULL` from `expires_at` alone would let a *login* token be minted with
    no expiry — an immortal interactive credential, produced by a one-line bug,
    with nothing failing. `api_tokens_only_keys_are_immortal` scopes "may live
    forever" to API keys, in the database.
  - **The pairing is 1:1**, enforced by the partial unique index
    `api_tokens_one_key_per_service_user` — keyed on `user_id` alone, because
    `(user_id, api_key_name)` would permit the many-keys-per-principal model
    that overlapping-key rotation needs and this design defers. Everything
    therefore addresses a key by its **principal's id**: `api_tokens`' primary
    key is `token_sha256`, which is credential material and must never travel
    in a URL or a log line.
  - **Rule 1 — a key never reaches an admin route.** `require_admin()`'s bearer
    branch refuses `user.is_api_key` **before** consulting `is_admin`. The
    guard sits at the point of use, not at mint time, because a service user can
    be promoted after its key was minted. `users.set_admin` also refuses to
    promote a service row, but the runtime gate is what carries the invariant —
    its test promotes by direct SQL precisely because the UI will not.
  - **Rule 2 — a service user cannot log in.** Three lookups verify a password
    against `api_users` (`api/auth.py::login`,
    `api/admin/auth.py::authenticate_admin`, the OAuth consent router), and they
    carried the `disabled_at IS NULL` wording by copy. The pure
    [src/localmail/api/login_eligible_sql.py](src/localmail/api/login_eligible_sql.py)
    is now the one authority, adding `is_service IS FALSE`. The unusable random
    password hash is *not* the protection — `users.set_password` is one admin
    click from making it usable, which is why that too refuses a service row.
  - **Revoke and delete are separate operations, deliberately.** `revoke_key`
    drops the token and keeps the principal, so re-minting under the same name
    restores service with the grants intact — that is the rotation path, and it
    is why `list_keys` is driven from `api_users` rather than `api_tokens` (a
    revoked bot holds no token row and must stay visible).
    `delete_key_principal` removes the bot; its `is_service IS TRUE` predicate
    is load-bearing, since the route is addressed by user id and would
    otherwise become a second way to delete a person.
  - **`create_key` runs in one transaction.** A failure after the principal is
    created would leave a row that the operator's retry then collides with.
````

Finally, update the migrations line under **Conventions** to name `0036` as the latest and `0037_*.sql` as the next free slot, and update the `migrations/` line in the **Layout** block the same way.

- [ ] **Step 4: Verify the tree is still green**

Run: `unset VIRTUAL_ENV && uv run pytest -q`
Expected: the full suite passes.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/mcp-usage.md CLAUDE.md
git commit -m "docs: API keys in README, MCP usage, and CLAUDE.md"
```

---

## Final verification

- [ ] **Full suite**

Run: `unset VIRTUAL_ENV && uv run pytest -q`
Expected: all green.

- [ ] **Type check**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: no new errors versus `main`.

- [ ] **Open the PR**

```bash
git push -u origin feat/admin-api-keys
gh pr create --base main --title "feat(admin): admin-issued API keys" --body "$(cat <<'EOF'
Implements docs/superpowers/specs/2026-08-24-admin-api-keys-design.md.

An admin names a machine consumer and receives an opaque `lmk_…` key once; the
key authenticates `/v1/*` and `/mcp` until revoked, reads only its granted
accounts, and is refused at every admin route.

The key is an `api_tokens` row minted against a dedicated service user, so the
per-account ACL and all three revocation levers reach it unchanged rather than
needing a fifth credential kind of their own.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Stop at PR-open with CI green; the operator merges.
