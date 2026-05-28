# Admin UI — Sub-plan 1: Admin auth + scaffolding

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the foundation for the admin UI — an `is_admin` column on `api_users`, cookie-session auth scoped to `/admin`, CSRF token plumbing, an empty Jinja2 + HTMX base layout, a login page, an authenticated dashboard placeholder, and CLI commands to grant/revoke admin. End state: an operator can run `localmail grant-admin USERNAME`, point their browser at `https://<server>/admin/login`, sign in, and see a "Hello, USER" dashboard with a logout button. No business actions yet — those land in Sub-plans 2–6.

**Architecture:** Two service modules under `localmail.api.admin` (`session_tokens`, `csrf`, `auth`) plus a FastAPI router family under `localmail.serve.admin` (`auth_router`, `dashboard_router`, `dependencies`, `middleware`). Sessions are HMAC-signed JSON payloads stored in an `HttpOnly; Secure; SameSite=Lax; Path=/admin` cookie. CSRF tokens are HMAC-bound to the session id. Templates rendered server-side with Jinja2; HTMX vendored as a static asset.

**Tech Stack:** Python 3.12, psycopg v3 + raw SQL, FastAPI, Jinja2, argon2-cffi (existing), HTMX (vendored), pytest, click.

**Spec:** [docs/superpowers/specs/2026-05-28-admin-ui-design.md](../specs/2026-05-28-admin-ui-design.md)

---

## File structure landing in this sub-plan

```
migrations/
  0021_api_users_admin.sql
src/localmail/
  config.py                                # +session_signing_key, +state_signing_key, +oauth_callback_url
  cli.py                                   # +grant-admin, +revoke-admin, +add-api-user --admin
  api/admin/
    __init__.py                            # package marker
    session_tokens.py                      # HMAC sign/verify for cookie session
    csrf.py                                # HMAC sign/verify for CSRF tokens
    auth.py                                # authenticate_admin(), get_admin_user(), grant/revoke
  serve/admin/
    __init__.py                            # build_admin_router(), build_admin_app_extensions()
    dependencies.py                        # require_admin_session FastAPI dep
    middleware.py                          # access-log scrubber, CSRF check
    auth_router.py                         # GET /admin/login, POST /admin/login, POST /admin/logout
    dashboard_router.py                    # GET /admin/
    templates/
      base.html
      login.html
      dashboard.html
    static/
      admin.css
      htmx.min.js                          # vendored from https://unpkg.com/htmx.org@2.x/dist/htmx.min.js
  serve/app.py                             # mount admin router + static + middleware
tests/
  test_admin_session_tokens.py
  test_admin_csrf.py
  test_admin_auth_service.py
  test_admin_auth_cli.py
  test_serve_admin_login.py
  test_serve_admin_logout.py
  test_serve_admin_dashboard.py
  test_serve_admin_middleware.py
pyproject.toml                             # +jinja2 dep
```

---

## Conventions (read once before starting)

- Every task is TDD-shaped: **failing test → minimal impl → passing test → commit**.
- DB tests use the existing `db_conn` fixture; it truncates `api_users` between tests.
- `api_users.password_hash` is argon2id; reuse `localmail.api.auth.hash_password` / `verify_password`.
- Empty `Optional[str]` config fields must **fail loudly at startup**, not auto-generate, so a regenerated key doesn't silently invalidate sessions.
- Commit after every passing test. No "I'll commit at the end".
- All file paths are absolute under `/Users/hherb/src/localmail/`.

---

## Task 1: Add `jinja2` and `itsdangerous` dependencies

**Why:** FastAPI's `Jinja2Templates` needs `jinja2` at runtime; we'll use it for `base.html`, `login.html`, `dashboard.html`. `itsdangerous` is **not** added — we roll our own HMAC primitives (16 lines) so we control the wire format exactly and don't depend on a library whose security defaults we'd need to audit. Listing this as a single explicit task so the engineer doesn't forget.

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add jinja2 to `dependencies` in `pyproject.toml`**

Find the `[project]` `dependencies = [...]` block, and after the existing `"httpx>=0.27",` line add:

```toml
    "jinja2>=3.1",
```

- [ ] **Step 2: Sync the lockfile**

Run: `unset VIRTUAL_ENV && uv sync`
Expected: no errors, `uv.lock` updated.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add jinja2 dep for admin UI templates"
```

---

## Task 2: Migration `0021_api_users_admin.sql`

**Why:** Adds the `is_admin` boolean that gates every admin route. Partial index keeps `WHERE is_admin` lookups cheap.

**Files:**
- Create: `migrations/0021_api_users_admin.sql`
- Test: `tests/test_migration_0021.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_0021.py`:

```python
"""Migration 0021 adds api_users.is_admin and a partial index on it."""
from __future__ import annotations

import psycopg


def test_is_admin_column_exists(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'api_users' AND column_name = 'is_admin'"
        )
        row = cur.fetchone()
    assert row is not None, "is_admin column missing from api_users"
    name, dtype, nullable, default = row
    assert dtype == "boolean"
    assert nullable == "NO"
    assert default == "false"


def test_partial_index_on_is_admin(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'api_users' AND indexname = 'api_users_is_admin_idx'"
        )
        row = cur.fetchone()
    assert row is not None, "api_users_is_admin_idx missing"
    indexdef = row[0]
    assert "WHERE" in indexdef and "is_admin" in indexdef
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_migration_0021.py -v`
Expected: FAIL with `is_admin column missing from api_users`.

- [ ] **Step 3: Write the migration**

Create `migrations/0021_api_users_admin.sql`:

```sql
-- Admin gate for /admin/* and /v1/admin/*. Bootstrap via shell-only CLI
-- (`localmail grant-admin USERNAME`); the column defaults to FALSE so
-- existing api_users keep their per-account-ACL-only privileges.

ALTER TABLE api_users
  ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- Partial index: admins are the rare case. Lookups are
-- `SELECT * FROM api_users WHERE id = ? AND is_admin = TRUE` from the
-- requires-admin dependency on every admin request.
CREATE INDEX api_users_is_admin_idx ON api_users (id) WHERE is_admin;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_migration_0021.py -v`
Expected: 2 passed.

- [ ] **Step 5: Verify migration runner applies it**

Run: `unset VIRTUAL_ENV && uv run localmail init-db`
Expected: "applying 0021_api_users_admin.sql" in stdout (or similar wording matching the existing runner).

- [ ] **Step 6: Commit**

```bash
git add migrations/0021_api_users_admin.sql tests/test_migration_0021.py
git commit -m "feat(migrations): 0021 add api_users.is_admin + partial index"
```

---

## Task 3: HMAC session token primitives

**Why:** Cookie-session auth needs a stateless signed token. Roll a 30-line module so the wire format is exactly what the spec describes and the threat model is auditable in one file.

Wire format: `base64url(json(payload)) + "." + base64url(hmac_sha256(key, base64url(json(payload))))`.
Payload: `{"v": 1, "user_id": int, "issued_at": int, "exp": int}`.

**Files:**
- Create: `src/localmail/api/admin/__init__.py`
- Create: `src/localmail/api/admin/session_tokens.py`
- Test: `tests/test_admin_session_tokens.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_session_tokens.py`:

```python
"""HMAC sign/verify for admin cookie-session tokens."""
from __future__ import annotations

import time

import pytest

from localmail.api.admin.session_tokens import (
    SessionPayload,
    SessionTokenError,
    decode_session_token,
    encode_session_token,
)

KEY = b"a" * 32


def test_round_trip() -> None:
    issued = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=issued, exp=issued + 3600)
    token = encode_session_token(payload, key=KEY)
    decoded = decode_session_token(token, key=KEY)
    assert decoded == payload


def test_tamper_in_payload_rejected() -> None:
    issued = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=issued, exp=issued + 3600)
    token = encode_session_token(payload, key=KEY)
    # Flip one character in the payload portion.
    body, sig = token.split(".")
    tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + sig
    with pytest.raises(SessionTokenError):
        decode_session_token(tampered, key=KEY)


def test_tamper_in_signature_rejected() -> None:
    issued = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=issued, exp=issued + 3600)
    token = encode_session_token(payload, key=KEY)
    body, sig = token.split(".")
    tampered = body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")
    with pytest.raises(SessionTokenError):
        decode_session_token(tampered, key=KEY)


def test_wrong_key_rejected() -> None:
    issued = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=issued, exp=issued + 3600)
    token = encode_session_token(payload, key=KEY)
    with pytest.raises(SessionTokenError):
        decode_session_token(token, key=b"b" * 32)


def test_expired_token_rejected() -> None:
    now = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=now - 10, exp=now - 1)
    token = encode_session_token(payload, key=KEY)
    with pytest.raises(SessionTokenError, match="expired"):
        decode_session_token(token, key=KEY, now=now)


def test_malformed_input_rejected() -> None:
    for bad in ["", "no-dot", "a.b.c", "...", "!!.!!"]:
        with pytest.raises(SessionTokenError):
            decode_session_token(bad, key=KEY)


def test_unknown_version_rejected() -> None:
    """A future-version token (v=2) must be rejected, not silently parsed."""
    from localmail.api.admin.session_tokens import _encode_unsigned, _sign

    body = _encode_unsigned({"v": 2, "user_id": 1, "issued_at": 0, "exp": 99999999999})
    sig = _sign(body, KEY)
    with pytest.raises(SessionTokenError, match="version"):
        decode_session_token(f"{body}.{sig}", key=KEY)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_session_tokens.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'localmail.api.admin'`.

- [ ] **Step 3: Create the package marker**

Create `src/localmail/api/admin/__init__.py`:

```python
"""Admin-only service layer for localmail.

Every public function here checks the caller's admin status at the service
boundary so it remains safe to import directly from a future MCP-admin or
scripting layer (no HTTP middleware required).
"""
```

- [ ] **Step 4: Implement `session_tokens.py`**

Create `src/localmail/api/admin/session_tokens.py`:

```python
"""HMAC-signed JSON payloads for admin cookie sessions.

Wire format:
    base64url(json(payload)) + "." + base64url(hmac_sha256(key, body))

The payload is a fixed-schema dict; unknown versions are rejected so we
can rotate format atomically.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any


_CURRENT_VERSION = 1


class SessionTokenError(Exception):
    """Any verify-side failure: tamper, expiry, malformed, wrong version."""


@dataclass(frozen=True)
class SessionPayload:
    user_id: int
    issued_at: int
    exp: int


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _encode_unsigned(d: dict[str, Any]) -> str:
    raw = json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _b64url_encode(raw)


def _sign(body_b64: str, key: bytes) -> str:
    mac = hmac.new(key, body_b64.encode("ascii"), sha256).digest()
    return _b64url_encode(mac)


def encode_session_token(payload: SessionPayload, *, key: bytes) -> str:
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise ValueError("key must be at least 16 bytes")
    body = _encode_unsigned({
        "v": _CURRENT_VERSION,
        "user_id": payload.user_id,
        "issued_at": payload.issued_at,
        "exp": payload.exp,
    })
    sig = _sign(body, key)
    return f"{body}.{sig}"


def decode_session_token(
    token: str,
    *,
    key: bytes,
    now: int | None = None,
) -> SessionPayload:
    if not isinstance(token, str) or token.count(".") != 1:
        raise SessionTokenError("malformed token")
    body, sig = token.split(".")
    if not body or not sig:
        raise SessionTokenError("malformed token")
    expected = _sign(body, key)
    if not hmac.compare_digest(expected, sig):
        raise SessionTokenError("signature mismatch")
    try:
        d = json.loads(_b64url_decode(body))
    except Exception as exc:
        raise SessionTokenError("malformed payload") from exc
    if not isinstance(d, dict):
        raise SessionTokenError("malformed payload")
    if d.get("v") != _CURRENT_VERSION:
        raise SessionTokenError(f"unsupported token version: {d.get('v')!r}")
    try:
        payload = SessionPayload(
            user_id=int(d["user_id"]),
            issued_at=int(d["issued_at"]),
            exp=int(d["exp"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionTokenError("malformed payload") from exc
    current = now if now is not None else int(time.time())
    if payload.exp <= current:
        raise SessionTokenError("expired")
    return payload
```

- [ ] **Step 5: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_session_tokens.py -v`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/api/admin/__init__.py \
        src/localmail/api/admin/session_tokens.py \
        tests/test_admin_session_tokens.py
git commit -m "feat(admin): HMAC-signed session tokens for cookie auth"
```

---

## Task 4: CSRF token primitives

**Why:** Every non-GET admin form/HTMX request carries a CSRF token bound to the session. Same HMAC primitives as Task 3 but the payload binds `(session_user_id, form_action)` so a token minted for `/admin/accounts/new` can't be replayed against `/admin/accounts/{id}/delete`.

**Files:**
- Create: `src/localmail/api/admin/csrf.py`
- Test: `tests/test_admin_csrf.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_csrf.py`:

```python
"""CSRF tokens bound to (session_user_id, action_key)."""
from __future__ import annotations

import pytest

from localmail.api.admin.csrf import CSRFError, make_csrf_token, verify_csrf_token

KEY = b"a" * 32


def test_round_trip() -> None:
    tok = make_csrf_token(user_id=7, action="/admin/accounts/new", key=KEY)
    verify_csrf_token(tok, user_id=7, action="/admin/accounts/new", key=KEY)  # no raise


def test_wrong_user_rejected() -> None:
    tok = make_csrf_token(user_id=7, action="/admin/accounts/new", key=KEY)
    with pytest.raises(CSRFError):
        verify_csrf_token(tok, user_id=8, action="/admin/accounts/new", key=KEY)


def test_wrong_action_rejected() -> None:
    tok = make_csrf_token(user_id=7, action="/admin/accounts/new", key=KEY)
    with pytest.raises(CSRFError):
        verify_csrf_token(tok, user_id=7, action="/admin/daemon/start", key=KEY)


def test_wrong_key_rejected() -> None:
    tok = make_csrf_token(user_id=7, action="/admin/x", key=KEY)
    with pytest.raises(CSRFError):
        verify_csrf_token(tok, user_id=7, action="/admin/x", key=b"b" * 32)


def test_malformed_rejected() -> None:
    for bad in ["", "no-dot", "a.b.c"]:
        with pytest.raises(CSRFError):
            verify_csrf_token(bad, user_id=7, action="/admin/x", key=KEY)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_csrf.py -v`
Expected: collection error — module not found.

- [ ] **Step 3: Implement `csrf.py`**

Create `src/localmail/api/admin/csrf.py`:

```python
"""CSRF tokens for admin forms.

Bound to (user_id, action) so a token minted for one form is useless for
another. No expiry — the cookie session itself expires; once it does, the
session middleware redirects to /admin/login before CSRF is checked.
"""
from __future__ import annotations

import hmac
from hashlib import sha256

from localmail.api.admin.session_tokens import _b64url_encode


class CSRFError(Exception):
    """Tamper, wrong user, wrong action, or malformed token."""


def _bind_string(user_id: int, action: str) -> bytes:
    return f"v=1|u={user_id}|a={action}".encode("utf-8")


def make_csrf_token(*, user_id: int, action: str, key: bytes) -> str:
    bound = _bind_string(user_id, action)
    mac = hmac.new(key, bound, sha256).digest()
    return _b64url_encode(mac)


def verify_csrf_token(
    token: str,
    *,
    user_id: int,
    action: str,
    key: bytes,
) -> None:
    if not isinstance(token, str) or not token or "." in token:
        raise CSRFError("malformed")
    expected = make_csrf_token(user_id=user_id, action=action, key=key)
    if not hmac.compare_digest(expected, token):
        raise CSRFError("mismatch")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_csrf.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/csrf.py tests/test_admin_csrf.py
git commit -m "feat(admin): CSRF tokens bound to (user_id, action)"
```

---

## Task 5: Config additions

**Why:** Three new `ServeConfig` fields land now (admin needs the session key; OAuth state key and callback URL ship in Sub-plan 4 but are added to the config schema now so config files are stable). All three are required when the admin UI is active — missing keys must **fail loudly at startup**, not auto-generate.

Conditional-required semantics: if any admin route is reachable, both signing keys must be set. We enforce this in `serve.app.create_app` (Task 17) rather than as a pydantic validator, because the validator runs even for the `daemon` and one-shot CLI codepaths that don't need the keys.

**Files:**
- Modify: `src/localmail/config.py:44-54` (the `ServeConfig` class body)
- Modify: `config.example.toml`
- Test: `tests/test_config_serve_admin.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_serve_admin.py`:

```python
"""ServeConfig carries the admin signing keys + oauth callback URL."""
from __future__ import annotations

import pytest

from localmail.config import ServeConfig


def test_defaults_are_empty_strings() -> None:
    cfg = ServeConfig()
    assert cfg.session_signing_key == ""
    assert cfg.state_signing_key == ""
    assert cfg.oauth_callback_url == ""


def test_explicit_values_are_kept() -> None:
    cfg = ServeConfig(
        session_signing_key="x" * 43,
        state_signing_key="y" * 43,
        oauth_callback_url="https://localmail.example.com/admin/oauth/callback",
    )
    assert cfg.session_signing_key == "x" * 43
    assert cfg.state_signing_key == "y" * 43
    assert cfg.oauth_callback_url.endswith("/admin/oauth/callback")


@pytest.mark.parametrize("field", ["session_signing_key", "state_signing_key"])
def test_short_keys_rejected(field: str) -> None:
    """Keys shorter than 32 bytes (base64url ~ 43 chars) are footguns."""
    kwargs = {field: "tooshort"}
    with pytest.raises(ValueError):
        ServeConfig(**kwargs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config_serve_admin.py -v`
Expected: 4 failures — fields don't exist yet.

- [ ] **Step 3: Add the fields to `ServeConfig`**

Open `src/localmail/config.py`. Find the `ServeConfig` class (around line 44) and append after `changes_safe_horizon_s`:

```python
    # Admin UI signing keys. Empty default = admin UI disabled; populated
    # only when the operator opts in by setting them in config.toml. Both
    # keys must be at least 32 base64url characters (~24 bytes decoded).
    # See docs/superpowers/specs/2026-05-28-admin-ui-design.md §3.
    session_signing_key: str = ""
    state_signing_key: str = ""

    # OAuth2 callback URL the admin UI redirects to after Google consent.
    # Must match an Authorized redirect URI registered in Google Cloud
    # Console for the localmail OAuth client. Empty default = OAuth web
    # flow disabled; CLI desktop loopback flow remains available.
    oauth_callback_url: str = ""

    @field_validator("session_signing_key", "state_signing_key")
    @classmethod
    def _validate_signing_key(cls, v: str) -> str:
        if v == "":
            return v
        if len(v) < 32:
            raise ValueError(
                "signing key must be at least 32 characters "
                "(generate with `python -c 'import secrets; "
                "print(secrets.token_urlsafe(32))'`)"
            )
        return v
```

You'll also need to add `field_validator` to the imports at the top of the file if it isn't already imported. Find the `from pydantic import …` line and ensure `field_validator` is in the list.

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config_serve_admin.py -v`
Expected: 4 passed.

- [ ] **Step 5: Update `config.example.toml`**

In `config.example.toml`, find the `[serve]` block (or add one if missing — search for `pool_max_size` to find existing ServeConfig docs). Append (or create) this block:

```toml
[serve]
# Admin UI signing keys. Required when admin UI is in use.
# Generate each with: python -c "import secrets; print(secrets.token_urlsafe(32))"
# session_signing_key = "..."
# state_signing_key   = "..."
# oauth_callback_url  = "https://localmail.example.com/admin/oauth/callback"
```

If `[serve]` already exists in the example file, append the three commented keys to it instead of duplicating the header.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/config.py config.example.toml tests/test_config_serve_admin.py
git commit -m "feat(config): ServeConfig session/state signing keys + oauth callback"
```

---

## Task 6: Admin auth service

**Why:** The transport-free service layer that the FastAPI router (Task 14) and the CLI commands (Tasks 7–8) both call. Three functions: authenticate (username+password → user row or `AuthenticationFailed`), look up by user_id with `is_admin` check, and grant/revoke admin.

**Files:**
- Create: `src/localmail/api/admin/auth.py`
- Test: `tests/test_admin_auth_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_auth_service.py`:

```python
"""Admin auth service: authenticate, get_admin_user, grant/revoke."""
from __future__ import annotations

import psycopg
import pytest

from localmail.api.admin.auth import (
    AdminUser,
    NotAnAdmin,
    UserNotFound,
    authenticate_admin,
    get_admin_user,
    grant_admin,
    revoke_admin,
)
from localmail.api.auth import hash_password
from localmail.api.errors import AuthenticationFailed


def _insert_user(conn: psycopg.Connection, username: str, password: str, *, is_admin: bool) -> int:
    pwh = hash_password(password)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, %s) RETURNING id",
            (username, pwh, is_admin),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_authenticate_admin_success(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    user = authenticate_admin(db_conn, username="horst", password="hunter2")
    assert user == AdminUser(id=uid, username="horst")


def test_authenticate_admin_wrong_password(db_conn: psycopg.Connection) -> None:
    _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    with pytest.raises(AuthenticationFailed):
        authenticate_admin(db_conn, username="horst", password="wrong")


def test_authenticate_admin_unknown_user(db_conn: psycopg.Connection) -> None:
    with pytest.raises(AuthenticationFailed):
        authenticate_admin(db_conn, username="ghost", password="any")


def test_authenticate_admin_non_admin_rejected(db_conn: psycopg.Connection) -> None:
    _insert_user(db_conn, "regular", "hunter2", is_admin=False)
    with pytest.raises(NotAnAdmin):
        authenticate_admin(db_conn, username="regular", password="hunter2")


def test_get_admin_user_success(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    assert get_admin_user(db_conn, user_id=uid) == AdminUser(id=uid, username="horst")


def test_get_admin_user_unknown(db_conn: psycopg.Connection) -> None:
    with pytest.raises(UserNotFound):
        get_admin_user(db_conn, user_id=9999)


def test_get_admin_user_non_admin(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "regular", "hunter2", is_admin=False)
    with pytest.raises(NotAnAdmin):
        get_admin_user(db_conn, user_id=uid)


def test_grant_admin_flips_flag(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "regular", "hunter2", is_admin=False)
    grant_admin(db_conn, username="regular")
    assert get_admin_user(db_conn, user_id=uid).username == "regular"


def test_grant_admin_idempotent(db_conn: psycopg.Connection) -> None:
    _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    grant_admin(db_conn, username="horst")  # already admin — no raise
    grant_admin(db_conn, username="horst")  # twice — still no raise


def test_grant_admin_unknown_user(db_conn: psycopg.Connection) -> None:
    with pytest.raises(UserNotFound):
        grant_admin(db_conn, username="ghost")


def test_revoke_admin_flips_flag(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    revoke_admin(db_conn, username="horst")
    with pytest.raises(NotAnAdmin):
        get_admin_user(db_conn, user_id=uid)


def test_revoke_admin_idempotent(db_conn: psycopg.Connection) -> None:
    _insert_user(db_conn, "regular", "hunter2", is_admin=False)
    revoke_admin(db_conn, username="regular")  # already non-admin — no raise


def test_revoke_admin_unknown_user(db_conn: psycopg.Connection) -> None:
    with pytest.raises(UserNotFound):
        revoke_admin(db_conn, username="ghost")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_auth_service.py -v`
Expected: collection error — module not found.

- [ ] **Step 3: Implement `auth.py`**

Create `src/localmail/api/admin/auth.py`:

```python
"""Admin user authentication and admin-grant management.

Service layer; takes a psycopg connection. Transport-free.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from localmail.api.auth import verify_password, _DUMMY_PASSWORD_HASH
from localmail.api.errors import AuthenticationFailed


class UserNotFound(Exception):
    """No api_users row with the given username/id."""


class NotAnAdmin(Exception):
    """User exists but is_admin = FALSE."""


@dataclass(frozen=True)
class AdminUser:
    id: int
    username: str


def authenticate_admin(
    conn: psycopg.Connection,
    *,
    username: str,
    password: str,
) -> AdminUser:
    """Verify credentials and return the admin user.

    Raises AuthenticationFailed if the username is unknown or the password
    is wrong. Raises NotAnAdmin if the credentials are valid but the user
    isn't an admin — kept distinct so the route handler can log it (a
    legitimate user trying the admin URL is different from an attacker).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, password_hash, is_admin FROM api_users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        # Constant-time path: run verify against a dummy hash to match the
        # response time of the wrong-password case (mirrors api/auth.py).
        verify_password(password, _DUMMY_PASSWORD_HASH)
        raise AuthenticationFailed()
    uid, pwh, is_admin = row
    if not verify_password(password, pwh):
        raise AuthenticationFailed()
    if not is_admin:
        raise NotAnAdmin()
    return AdminUser(id=int(uid), username=username)


def get_admin_user(conn: psycopg.Connection, *, user_id: int) -> AdminUser:
    """Look up an admin by id. Raises UserNotFound / NotAnAdmin."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT username, is_admin FROM api_users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user with id={user_id}")
    username, is_admin = row
    if not is_admin:
        raise NotAnAdmin(f"user {user_id} is not an admin")
    return AdminUser(id=user_id, username=username)


def grant_admin(conn: psycopg.Connection, *, username: str) -> None:
    """Set is_admin=TRUE for the named user. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET is_admin = TRUE WHERE username = %s RETURNING id",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user named {username!r}")
    conn.commit()


def revoke_admin(conn: psycopg.Connection, *, username: str) -> None:
    """Set is_admin=FALSE for the named user. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET is_admin = FALSE WHERE username = %s RETURNING id",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user named {username!r}")
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_auth_service.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/auth.py tests/test_admin_auth_service.py
git commit -m "feat(admin): authenticate_admin + grant/revoke service layer"
```

---

## Task 7: CLI `grant-admin` / `revoke-admin` / `add-api-user --admin`

**Why:** Shell-only admin bootstrap. The first admin can't be created from the UI (no admin to log in as), so a server-host CLI command is the only path.

**Files:**
- Modify: `src/localmail/cli.py` (after the existing `remove-api-user` command around line 927)
- Test: `tests/test_admin_auth_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_auth_cli.py`:

```python
"""CLI commands for admin grant/revoke and add-api-user --admin."""
from __future__ import annotations

import psycopg
from click.testing import CliRunner

from localmail.cli import main


def _is_admin(conn: psycopg.Connection, username: str) -> bool | None:
    with conn.cursor() as cur:
        cur.execute("SELECT is_admin FROM api_users WHERE username = %s", (username,))
        row = cur.fetchone()
    return None if row is None else bool(row[0])


def test_grant_admin_promotes_existing_user(db_conn: psycopg.Connection, tmp_path, monkeypatch) -> None:
    # add a non-admin user first via the existing CLI
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_conn.info.dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))

    runner = CliRunner()
    res = runner.invoke(main, ["add-api-user", "horst"], input="hunter2\nhunter2\n")
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is False

    res = runner.invoke(main, ["grant-admin", "horst"])
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is True


def test_revoke_admin_demotes(db_conn: psycopg.Connection, tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_conn.info.dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))

    runner = CliRunner()
    runner.invoke(main, ["add-api-user", "--admin", "horst"], input="hunter2\nhunter2\n")
    assert _is_admin(db_conn, "horst") is True

    res = runner.invoke(main, ["revoke-admin", "horst"])
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is False


def test_grant_admin_unknown_user_errors(tmp_path, monkeypatch, db_conn) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_conn.info.dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))

    runner = CliRunner()
    res = runner.invoke(main, ["grant-admin", "ghost"])
    assert res.exit_code != 0
    assert "no user named 'ghost'" in res.output


def test_add_api_user_admin_flag(db_conn: psycopg.Connection, tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_conn.info.dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))

    runner = CliRunner()
    res = runner.invoke(main, ["add-api-user", "--admin", "horst"], input="hunter2\nhunter2\n")
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_auth_cli.py -v`
Expected: failures around `grant-admin` / `revoke-admin` not being commands and `--admin` flag not recognized.

- [ ] **Step 3: Read the existing `add-api-user` command**

Run: `grep -n "add-api-user\b" src/localmail/cli.py`
Expected: a line near 873 pointing at `@main.command("add-api-user")`.

Read it so you have context for the change:

Run: `unset VIRTUAL_ENV && uv run python -c "import inspect, localmail.cli as c; print(inspect.getsource(c.add_api_user))"` — or just open the file and read the function around line 873.

- [ ] **Step 4: Add the `--admin` flag to `add-api-user`**

Find the `add-api-user` command in `src/localmail/cli.py`. Add a new option just before the function signature. The pattern in this file is `@click.option(...)` decorators stacked on `@main.command(...)`. Add:

```python
@click.option(
    "--admin",
    "is_admin",
    is_flag=True,
    default=False,
    help="Create the user with is_admin=TRUE (admin-UI bootstrap).",
)
```

Then in the function body, after the INSERT that creates the user, run an additional UPDATE if `is_admin`:

```python
    if is_admin:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_users SET is_admin = TRUE WHERE username = %s",
                (username,),
            )
        conn.commit()
```

(Adapt this to match the existing function's connection handling — it almost certainly already has a `conn` variable in scope.)

- [ ] **Step 5: Add `grant-admin` and `revoke-admin` commands**

Append to `src/localmail/cli.py` (after the existing `remove-api-user` command):

```python
@main.command("grant-admin")
@click.argument("username")
@click.pass_context
def grant_admin_cmd(ctx: click.Context, username: str) -> None:
    """Grant admin privileges to USERNAME (shell-only bootstrap path)."""
    from .api.admin.auth import UserNotFound, grant_admin

    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        try:
            grant_admin(conn, username=username)
        except UserNotFound as exc:
            raise click.ClickException(str(exc))
    click.echo(f"granted admin to {username!r}")


@main.command("revoke-admin")
@click.argument("username")
@click.pass_context
def revoke_admin_cmd(ctx: click.Context, username: str) -> None:
    """Revoke admin privileges from USERNAME."""
    from .api.admin.auth import UserNotFound, revoke_admin

    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        try:
            revoke_admin(conn, username=username)
        except UserNotFound as exc:
            raise click.ClickException(str(exc))
    click.echo(f"revoked admin from {username!r}")
```

- [ ] **Step 6: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_auth_cli.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/cli.py tests/test_admin_auth_cli.py
git commit -m "feat(cli): grant-admin / revoke-admin + add-api-user --admin"
```

---

## Task 8: FastAPI `require_admin_session` dependency

**Why:** Every admin route depends on `require_admin_session` to (a) read the cookie, (b) verify the HMAC, (c) check `is_admin = TRUE`, (d) return the user. Routes that pass this dep get a guaranteed `AdminUser` argument.

**Files:**
- Create: `src/localmail/serve/admin/__init__.py`
- Create: `src/localmail/serve/admin/dependencies.py`
- Test: `tests/test_serve_admin_dependencies.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_admin_dependencies.py`:

```python
"""require_admin_session dependency: cookie → AdminUser, else redirect/403."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from localmail.api.admin.auth import AdminUser
from localmail.api.admin.session_tokens import SessionPayload, encode_session_token
from localmail.api.auth import hash_password
from localmail.serve.admin.dependencies import (
    SESSION_COOKIE_NAME,
    require_admin_session,
)

KEY = b"a" * 32


def _make_app(pool: ConnectionPool, *, key: bytes = KEY) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.state.serve_config = type("Cfg", (), {
        "session_signing_key": key.decode("latin1"),
    })()

    @app.get("/admin/probe")
    def probe(user: AdminUser = require_admin_session()):  # type: ignore[assignment]
        return {"id": user.id, "username": user.username}

    return app


@pytest.fixture
def pool(db_dsn):
    p = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    yield p
    p.close()


def _seed_admin(pool: ConnectionPool, username: str = "horst") -> int:
    pwh = hash_password("hunter2")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_users (username, password_hash, is_admin) "
                "VALUES (%s, %s, TRUE) RETURNING id",
                (username, pwh),
            )
            row = cur.fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def test_no_cookie_redirects(pool: ConnectionPool) -> None:
    client = TestClient(_make_app(pool), follow_redirects=False)
    r = client.get("/admin/probe")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login")


def test_valid_cookie_admits(pool: ConnectionPool) -> None:
    uid = _seed_admin(pool)
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now, exp=now + 3600),
        key=KEY,
    )
    client = TestClient(_make_app(pool), follow_redirects=False)
    r = client.get("/admin/probe", cookies={SESSION_COOKIE_NAME: tok})
    assert r.status_code == 200
    assert r.json() == {"id": uid, "username": "horst"}


def test_tampered_cookie_redirects(pool: ConnectionPool) -> None:
    uid = _seed_admin(pool)
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now, exp=now + 3600),
        key=KEY,
    )
    # Flip one char in the signature
    body, sig = tok.split(".")
    tampered = body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")
    client = TestClient(_make_app(pool), follow_redirects=False)
    r = client.get("/admin/probe", cookies={SESSION_COOKIE_NAME: tampered})
    assert r.status_code == 303


def test_expired_cookie_redirects(pool: ConnectionPool) -> None:
    uid = _seed_admin(pool)
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now - 10, exp=now - 1),
        key=KEY,
    )
    client = TestClient(_make_app(pool), follow_redirects=False)
    r = client.get("/admin/probe", cookies={SESSION_COOKIE_NAME: tok})
    assert r.status_code == 303


def test_non_admin_user_403(pool: ConnectionPool) -> None:
    # Insert a real user, then revoke their admin status.
    uid = _seed_admin(pool, "regular")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_users SET is_admin = FALSE WHERE id = %s", (uid,))
        conn.commit()
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now, exp=now + 3600),
        key=KEY,
    )
    client = TestClient(_make_app(pool), follow_redirects=False)
    r = client.get("/admin/probe", cookies={SESSION_COOKIE_NAME: tok})
    assert r.status_code == 403


def test_user_deleted_after_cookie_issued_redirects(pool: ConnectionPool) -> None:
    uid = _seed_admin(pool)
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now, exp=now + 3600),
        key=KEY,
    )
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_users WHERE id = %s", (uid,))
        conn.commit()
    client = TestClient(_make_app(pool), follow_redirects=False)
    r = client.get("/admin/probe", cookies={SESSION_COOKIE_NAME: tok})
    assert r.status_code == 303
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_dependencies.py -v`
Expected: collection error — modules not found.

- [ ] **Step 3: Create the `serve/admin` package marker**

Create `src/localmail/serve/admin/__init__.py`:

```python
"""HTTP routes and middleware for the admin UI.

Routers built here are mounted by localmail.serve.app.create_app at the
/admin and /v1/admin prefixes.
"""
```

- [ ] **Step 4: Implement `dependencies.py`**

Create `src/localmail/serve/admin/dependencies.py`:

```python
"""FastAPI dependencies for admin routes.

require_admin_session() reads the session cookie, verifies its HMAC, looks
up the user, and asserts is_admin=TRUE. Failures redirect to /admin/login
(no cookie / tampered / expired / deleted) or return 403 (valid cookie,
user no longer admin).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from localmail.api.admin.auth import (
    AdminUser,
    NotAnAdmin,
    UserNotFound,
    get_admin_user,
)
from localmail.api.admin.session_tokens import (
    SessionTokenError,
    decode_session_token,
)


SESSION_COOKIE_NAME = "localmail_admin_session"


class _AdminRedirect(HTTPException):
    """Signals 'redirect to /admin/login'. Caught by app-wide handler."""
    def __init__(self) -> None:
        super().__init__(status_code=303, detail="redirect-to-login")


def _signing_key(request: Request) -> bytes:
    cfg = request.app.state.serve_config
    key_str = getattr(cfg, "session_signing_key", "")
    if not key_str:
        raise RuntimeError(
            "ServeConfig.session_signing_key is empty; admin UI requires it. "
            "Set [serve] session_signing_key in config.toml."
        )
    return key_str.encode("latin1") if isinstance(key_str, str) else key_str


def require_admin_session():
    """Dependency factory; returns the AdminUser or raises redirect/403."""
    def _dep(request: Request) -> AdminUser:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            raise _AdminRedirect()
        key = _signing_key(request)
        try:
            payload = decode_session_token(token, key=key)
        except SessionTokenError:
            raise _AdminRedirect()
        pool = request.app.state.pool
        with pool.connection() as conn:
            try:
                return get_admin_user(conn, user_id=payload.user_id)
            except UserNotFound:
                raise _AdminRedirect()
            except NotAnAdmin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="not an admin",
                )
    return Depends(_dep)


def install_admin_redirect_handler(app) -> None:
    """Translate the internal _AdminRedirect exception to a 303 redirect."""
    @app.exception_handler(_AdminRedirect)
    async def _on_redirect(_request, _exc):
        return RedirectResponse("/admin/login", status_code=303)
```

- [ ] **Step 5: Run test to verify it passes**

The tests call `require_admin_session()` but their fake app doesn't install the redirect handler. Add a small adjustment to the test setup — go back to the test file and modify `_make_app` to also wire the handler:

In `tests/test_serve_admin_dependencies.py`, change `_make_app` so it calls `install_admin_redirect_handler(app)` before returning. Add to the imports:

```python
from localmail.serve.admin.dependencies import install_admin_redirect_handler
```

And in `_make_app`, just before `return app`:

```python
    install_admin_redirect_handler(app)
```

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_dependencies.py -v`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/admin/__init__.py \
        src/localmail/serve/admin/dependencies.py \
        tests/test_serve_admin_dependencies.py
git commit -m "feat(admin): require_admin_session FastAPI dependency"
```

---

## Task 9: Access-log scrubber middleware

**Why:** OAuth callback URLs carry `code` and `state` query parameters; login form posts carry `password` in the body. We don't want any of those in access logs. Implement once now (used by sub-plans 3 and 4); test it with a dummy log capturer.

The middleware runs *before* uvicorn's default access log line is emitted: it rewrites `request.scope["query_string"]` to a scrubbed version for the duration of the request. The actual request handler still sees the originals via a separate accessor we add to the middleware (so the OAuth handler can read the real `code`).

**Files:**
- Create: `src/localmail/serve/admin/middleware.py`
- Test: `tests/test_serve_admin_middleware.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_admin_middleware.py`:

```python
"""Access-log scrubber rewrites query string for the access log only."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from localmail.serve.admin.middleware import (
    ScrubSensitiveQueryParamsMiddleware,
    get_unscrubbed_query_params,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        ScrubSensitiveQueryParamsMiddleware,
        sensitive=("code", "state", "password"),
    )

    @app.get("/echo")
    async def echo(request: Request):
        return {
            "scrubbed_query": request.url.query,
            "unscrubbed": dict(get_unscrubbed_query_params(request)),
        }

    return app


def test_sensitive_params_scrubbed_in_url() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo?code=secretcode&state=secretstate&keep=visible")
    j = r.json()
    assert "code=secretcode" not in j["scrubbed_query"]
    assert "state=secretstate" not in j["scrubbed_query"]
    assert "code=REDACTED" in j["scrubbed_query"]
    assert "state=REDACTED" in j["scrubbed_query"]
    assert "keep=visible" in j["scrubbed_query"]


def test_handler_still_sees_unscrubbed() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo?code=secretcode&keep=visible")
    j = r.json()
    assert j["unscrubbed"] == {"code": "secretcode", "keep": "visible"}


def test_no_query_string_is_no_op() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo")
    j = r.json()
    assert j["scrubbed_query"] == ""
    assert j["unscrubbed"] == {}


def test_non_sensitive_params_untouched() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo?account=horst&limit=10")
    j = r.json()
    assert "account=horst" in j["scrubbed_query"]
    assert "limit=10" in j["scrubbed_query"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_middleware.py -v`
Expected: collection error — module not found.

- [ ] **Step 3: Implement `middleware.py`**

Create `src/localmail/serve/admin/middleware.py`:

```python
"""ASGI middleware that rewrites sensitive query parameters before logging.

The request handler still sees the originals via get_unscrubbed_query_params,
because we stash a parsed copy in request.scope under a private key before
rewriting.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

from starlette.middleware.base import BaseHTTPMiddleware


_UNSCRUBBED_KEY = "localmail.admin.unscrubbed_query"


def get_unscrubbed_query_params(request) -> dict[str, str]:
    """Return the original (non-redacted) query params for this request."""
    return request.scope.get(_UNSCRUBBED_KEY, {})


class ScrubSensitiveQueryParamsMiddleware(BaseHTTPMiddleware):
    """Rewrites request.scope['query_string'] so subsequent access-log
    middleware sees REDACTED instead of secrets.

    Adds a copy of the original query params to request.scope under a
    private key so route handlers can still read the originals.
    """

    def __init__(self, app, *, sensitive: tuple[str, ...]) -> None:
        super().__init__(app)
        self._sensitive = set(sensitive)

    async def dispatch(self, request, call_next):
        raw = request.scope.get("query_string", b"")
        if raw:
            pairs = parse_qsl(raw.decode("latin1"), keep_blank_values=True)
            request.scope[_UNSCRUBBED_KEY] = dict(pairs)
            scrubbed = [
                (k, "REDACTED" if k in self._sensitive else v)
                for k, v in pairs
            ]
            request.scope["query_string"] = urlencode(scrubbed).encode("latin1")
        else:
            request.scope[_UNSCRUBBED_KEY] = {}
        return await call_next(request)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_middleware.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/middleware.py tests/test_serve_admin_middleware.py
git commit -m "feat(admin): query-param access-log scrubber middleware"
```

---

## Task 10: `base.html` layout template

**Why:** Every admin page inherits from this. Defines the nav, flash region, CSRF helper macro, and HTMX bootstrap.

**Files:**
- Create: `src/localmail/serve/admin/templates/base.html`

- [ ] **Step 1: Create the file**

Create `src/localmail/serve/admin/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}localmail admin{% endblock %}</title>
  <link rel="stylesheet" href="/admin/static/admin.css">
  <script src="/admin/static/htmx.min.js" defer></script>
</head>
<body{% if current_user %} hx-headers='{"X-CSRF-Token": "{{ csrf_token_for("htmx") }}"}'{% endif %}>
  <header class="admin-header">
    <a href="/admin/" class="admin-logo">localmail admin</a>
    {% if current_user %}
      <nav class="admin-nav">
        <a href="/admin/">Dashboard</a>
        <a href="/admin/accounts">Accounts</a>
        <a href="/admin/daemon">Daemon</a>
        <a href="/admin/imports">Imports</a>
        <a href="/admin/users">Users</a>
      </nav>
      <form method="post" action="/admin/logout" class="admin-logout-form">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_for('/admin/logout') }}">
        <span class="admin-user">{{ current_user.username }}</span>
        <button type="submit">Sign out</button>
      </form>
    {% endif %}
  </header>

  {% if flashes %}
    <ul class="admin-flashes">
      {% for category, message in flashes %}
        <li class="admin-flash admin-flash-{{ category }}">{{ message }}</li>
      {% endfor %}
    </ul>
  {% endif %}

  <main class="admin-main">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add src/localmail/serve/admin/templates/base.html
git commit -m "feat(admin): base.html layout with nav, flash, CSRF for HTMX"
```

---

## Task 11: `admin.css` minimal stylesheet

**Why:** Just enough to make the UI look like a tool rather than a wireframe. Operator-facing, low-frequency use; no design system needed.

**Files:**
- Create: `src/localmail/serve/admin/static/admin.css`

- [ ] **Step 1: Create the file**

Create `src/localmail/serve/admin/static/admin.css`:

```css
:root {
  --fg: #1a1a1a;
  --fg-muted: #555;
  --bg: #f8f8f7;
  --bg-card: #fff;
  --border: #d4d4d2;
  --accent: #2d5e9c;
  --danger: #b3261e;
  --ok: #2e7d32;
  --warn: #b9700f;
  --shadow: 0 1px 2px rgba(0,0,0,0.06);
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.45;
  color: var(--fg);
  background: var(--bg);
  margin: 0;
}
.admin-header {
  display: flex; align-items: center; gap: 24px;
  background: var(--bg-card);
  border-bottom: 1px solid var(--border);
  padding: 10px 20px;
}
.admin-logo { font-weight: 600; color: var(--accent); text-decoration: none; }
.admin-nav { display: flex; gap: 16px; flex: 1; }
.admin-nav a { color: var(--fg); text-decoration: none; }
.admin-nav a:hover { color: var(--accent); text-decoration: underline; }
.admin-logout-form { display: flex; align-items: center; gap: 8px; margin: 0; }
.admin-logout-form button {
  background: transparent; border: 1px solid var(--border);
  padding: 4px 12px; cursor: pointer; border-radius: 3px;
}
.admin-user { color: var(--fg-muted); }
.admin-main { padding: 24px; max-width: 1100px; margin: 0 auto; }
.admin-flashes { list-style: none; padding: 0 24px; margin: 12px auto; max-width: 1100px; }
.admin-flash { padding: 10px 14px; border-radius: 3px; margin-bottom: 6px; }
.admin-flash-success { background: #e6f4ea; color: var(--ok); border: 1px solid var(--ok); }
.admin-flash-error   { background: #fde7e9; color: var(--danger); border: 1px solid var(--danger); }
.admin-flash-warn    { background: #fff3e0; color: var(--warn); border: 1px solid var(--warn); }
.admin-card {
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 4px; padding: 16px; box-shadow: var(--shadow);
}
.admin-form label { display: block; margin: 12px 0 4px; }
.admin-form input, .admin-form select, .admin-form textarea {
  width: 100%; padding: 6px 8px; border: 1px solid var(--border);
  border-radius: 3px; font: inherit;
}
.admin-form button[type=submit] {
  margin-top: 16px; padding: 8px 18px;
  background: var(--accent); color: white; border: 0; border-radius: 3px;
  cursor: pointer;
}
.admin-login-card { max-width: 360px; margin: 60px auto; }
```

- [ ] **Step 2: Commit**

```bash
git add src/localmail/serve/admin/static/admin.css
git commit -m "feat(admin): minimal admin.css stylesheet"
```

---

## Task 12: Vendor `htmx.min.js`

**Why:** Server-rendered with HTMX. Vendoring avoids a runtime CDN dependency (operator's network can be locked down) and avoids the SRI maintenance burden of a CDN tag.

**Files:**
- Create: `src/localmail/serve/admin/static/htmx.min.js`

- [ ] **Step 1: Download HTMX 2.x**

Run:

```bash
curl -sL https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js \
  -o src/localmail/serve/admin/static/htmx.min.js
```

(Verify the version on https://htmx.org/ if 2.0.4 is no longer current — pick the latest 2.x.)

- [ ] **Step 2: Sanity-check the file**

Run: `wc -c src/localmail/serve/admin/static/htmx.min.js`
Expected: file size 40000–80000 bytes (HTMX minified).

Run: `head -c 200 src/localmail/serve/admin/static/htmx.min.js`
Expected: looks like minified JS (no HTML error page).

- [ ] **Step 3: Commit**

```bash
git add src/localmail/serve/admin/static/htmx.min.js
git commit -m "feat(admin): vendor htmx.min.js 2.0.x"
```

---

## Task 13: Login route + `login.html`

**Why:** The only unauthenticated admin route. Renders a form on GET, validates credentials + issues the session cookie on POST. CSRF token on the form is bound to a sentinel action `"/admin/login"` (which means: anyone hitting the POST without first having a GET-rendered form is rejected).

The session cookie is issued with `HttpOnly; Secure; SameSite=Lax; Path=/admin; Max-Age=28800` (8 hours).

**Files:**
- Create: `src/localmail/serve/admin/auth_router.py`
- Create: `src/localmail/serve/admin/templates/login.html`
- Test: `tests/test_serve_admin_login.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_admin_login.py`:

```python
"""GET/POST /admin/login: render form, validate creds, issue cookie."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.dependencies import SESSION_COOKIE_NAME
from localmail.serve.app import create_app


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key="x" * 43,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
    )


@pytest.fixture
def app(db_dsn, serve_cfg):
    return create_app(db_dsn=db_dsn, serve_config=serve_cfg)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def admin_user(db_conn: psycopg.Connection) -> int:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, TRUE) RETURNING id",
            ("horst", pwh),
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


def test_get_login_renders_form(client: TestClient) -> None:
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert "<form" in r.text and 'name="username"' in r.text and 'name="password"' in r.text
    assert 'name="csrf_token"' in r.text


def test_post_login_success_issues_cookie(client: TestClient, admin_user: int) -> None:
    form = client.get("/admin/login").text
    csrf = _extract_csrf(form)
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": csrf},
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/admin/"
    cookie = r.cookies.get(SESSION_COOKIE_NAME)
    assert cookie is not None


def test_post_login_wrong_password_re_renders_form(client: TestClient, admin_user: int) -> None:
    form = client.get("/admin/login").text
    csrf = _extract_csrf(form)
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "wrong", "csrf_token": csrf},
    )
    assert r.status_code == 401
    assert "invalid credentials" in r.text.lower()


def test_post_login_non_admin_rejected(client: TestClient, db_conn: psycopg.Connection) -> None:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, FALSE)",
            ("regular", pwh),
        )
    db_conn.commit()
    form = client.get("/admin/login").text
    csrf = _extract_csrf(form)
    r = client.post(
        "/admin/login",
        data={"username": "regular", "password": "hunter2", "csrf_token": csrf},
    )
    assert r.status_code == 403
    assert "admin" in r.text.lower()


def test_post_login_missing_csrf_rejected(client: TestClient, admin_user: int) -> None:
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2"},  # no csrf_token
    )
    assert r.status_code == 400


def test_post_login_bad_csrf_rejected(client: TestClient, admin_user: int) -> None:
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": "not-a-real-token"},
    )
    assert r.status_code == 400


def _extract_csrf(html: str) -> str:
    import re
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, f"no csrf_token in form html"
    return m.group(1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_login.py -v`
Expected: collection or import errors — login route not wired yet.

- [ ] **Step 3: Write `login.html`**

Create `src/localmail/serve/admin/templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}Sign in — localmail admin{% endblock %}
{% block content %}
<div class="admin-card admin-login-card">
  <h1>Sign in</h1>
  {% if error %}
    <p class="admin-flash admin-flash-error">{{ error }}</p>
  {% endif %}
  <form method="post" action="/admin/login" class="admin-form">
    <label for="username">Username</label>
    <input type="text" id="username" name="username" required autocomplete="username">
    <label for="password">Password</label>
    <input type="password" id="password" name="password" required autocomplete="current-password">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button type="submit">Sign in</button>
  </form>
</div>
{% endblock %}
```

- [ ] **Step 4: Implement the router**

Create `src/localmail/serve/admin/auth_router.py`:

```python
"""GET /admin/login (render form), POST /admin/login (validate + cookie)."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from localmail.api.admin.auth import (
    AdminUser,
    NotAnAdmin,
    authenticate_admin,
)
from localmail.api.admin.csrf import CSRFError, make_csrf_token, verify_csrf_token
from localmail.api.admin.session_tokens import SessionPayload, encode_session_token
from localmail.api.errors import AuthenticationFailed
from localmail.serve.admin.dependencies import SESSION_COOKIE_NAME

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

LOGIN_CSRF_ACTION = "/admin/login"
SESSION_TTL_SECONDS = 8 * 3600


def _session_key(request: Request) -> bytes:
    cfg = request.app.state.serve_config
    s_key = cfg.session_signing_key
    if not s_key:
        raise RuntimeError("session_signing_key is empty; admin UI disabled")
    return s_key.encode("latin1") if isinstance(s_key, str) else s_key


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def get_login(request: Request) -> HTMLResponse:
    s_key = _session_key(request)
    # CSRF token bound to user_id=0 (anonymous) + action="/admin/login".
    # The verify path uses the same anonymous binding.
    csrf = make_csrf_token(user_id=0, action=LOGIN_CSRF_ACTION, key=s_key)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": csrf, "current_user": None, "flashes": []},
    )


@router.post("/login")
def post_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    s_key = _session_key(request)
    try:
        verify_csrf_token(csrf_token, user_id=0, action=LOGIN_CSRF_ACTION, key=s_key)
    except CSRFError:
        return HTMLResponse("CSRF token missing or invalid", status_code=400)

    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            admin = authenticate_admin(conn, username=username, password=password)
        except AuthenticationFailed:
            csrf = make_csrf_token(user_id=0, action=LOGIN_CSRF_ACTION, key=s_key)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": csrf,
                    "error": "Invalid credentials.",
                    "current_user": None,
                    "flashes": [],
                },
                status_code=401,
            )
        except NotAnAdmin:
            csrf = make_csrf_token(user_id=0, action=LOGIN_CSRF_ACTION, key=s_key)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": csrf,
                    "error": "This account is not an admin.",
                    "current_user": None,
                    "flashes": [],
                },
                status_code=403,
            )

    now = int(time.time())
    token = encode_session_token(
        SessionPayload(user_id=admin.id, issued_at=now, exp=now + SESSION_TTL_SECONDS),
        key=s_key,
    )
    response = RedirectResponse("/admin/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/admin",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response
```

- [ ] **Step 5: Wire the router and `csrf_token_for` template helper into `create_app`**

This task is incomplete until Task 17 wires the router into `create_app`. The test will fail until then. **Note this in your plan progress** and proceed to Task 14 (logout) and Task 15 (dashboard) before running this task's test.

- [ ] **Step 6: Commit the login pieces (router + template) only**

```bash
git add src/localmail/serve/admin/auth_router.py \
        src/localmail/serve/admin/templates/login.html \
        tests/test_serve_admin_login.py
git commit -m "feat(admin): /admin/login route + template (test pending Task 17 wiring)"
```

---

## Task 14: Logout route

**Why:** Drops the session cookie. Form submit on `base.html` already wired to `POST /admin/logout` with a CSRF token bound to action `"/admin/logout"`.

**Files:**
- Modify: `src/localmail/serve/admin/auth_router.py`
- Test: `tests/test_serve_admin_logout.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_admin_logout.py`:

```python
"""POST /admin/logout clears the session cookie."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.dependencies import SESSION_COOKIE_NAME
from localmail.serve.app import create_app


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key="x" * 43,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
    )


@pytest.fixture
def client(db_dsn, serve_cfg):
    return TestClient(create_app(db_dsn=db_dsn, serve_config=serve_cfg), follow_redirects=False)


def _login(client: TestClient, db_conn: psycopg.Connection) -> None:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, TRUE)",
            ("horst", pwh),
        )
    db_conn.commit()
    import re
    form = client.get("/admin/login").text
    csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', form).group(1)
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": csrf},
    )
    assert r.status_code == 303
    # TestClient persists cookies across requests on the same client.


def test_logout_clears_cookie(client: TestClient, db_conn: psycopg.Connection, serve_cfg) -> None:
    _login(client, db_conn)
    s_key = serve_cfg.session_signing_key.encode("latin1")
    # CSRF for logout binds to the logged-in user (user_id from the session).
    # The cleanest fetch path: GET / and parse the form action button's csrf.
    # For test simplicity, we mint a token matching the seeded user (id=1 after truncate).
    csrf = make_csrf_token(user_id=1, action="/admin/logout", key=s_key)
    r = client.post("/admin/logout", data={"csrf_token": csrf})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"
    # The Set-Cookie header should have Max-Age=0 or an expires-in-the-past
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert ("max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_logout.py -v`
Expected: 404 — route not defined yet.

- [ ] **Step 3: Add the logout route**

Append to `src/localmail/serve/admin/auth_router.py`:

```python
from localmail.serve.admin.dependencies import require_admin_session


LOGOUT_CSRF_ACTION = "/admin/logout"


@router.post("/logout")
def post_logout(
    request: Request,
    csrf_token: str = Form(""),
    admin: AdminUser = require_admin_session(),
):
    s_key = _session_key(request)
    try:
        verify_csrf_token(csrf_token, user_id=admin.id, action=LOGOUT_CSRF_ACTION, key=s_key)
    except CSRFError:
        return HTMLResponse("CSRF token missing or invalid", status_code=400)

    response = RedirectResponse("/admin/login", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        "",
        max_age=0,
        path="/admin",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response
```

- [ ] **Step 4: Note the dependency on Task 17**

Like the login test, the logout test cannot pass until Task 17 wires the router into the FastAPI app. Proceed to Task 15.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/auth_router.py tests/test_serve_admin_logout.py
git commit -m "feat(admin): /admin/logout route (test pending Task 17 wiring)"
```

---

## Task 15: Dashboard placeholder route + `dashboard.html`

**Why:** Smallest possible "you are authenticated" page so the smoke test in Task 17 has somewhere to land. Real dashboard content arrives in Sub-plans 3 / 5 / 6.

**Files:**
- Create: `src/localmail/serve/admin/dashboard_router.py`
- Create: `src/localmail/serve/admin/templates/dashboard.html`
- Test: `tests/test_serve_admin_dashboard.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_admin_dashboard.py`:

```python
"""GET /admin/ — authenticated dashboard placeholder."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.app import create_app


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key="x" * 43,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
    )


@pytest.fixture
def client(db_dsn, serve_cfg):
    return TestClient(create_app(db_dsn=db_dsn, serve_config=serve_cfg), follow_redirects=False)


def test_dashboard_redirects_when_unauthenticated(client: TestClient) -> None:
    r = client.get("/admin/")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login")


def test_dashboard_renders_when_authenticated(client: TestClient, db_conn: psycopg.Connection) -> None:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, TRUE)",
            ("horst", pwh),
        )
    db_conn.commit()
    import re
    form = client.get("/admin/login").text
    csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', form).group(1)
    client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": csrf},
    )
    r = client.get("/admin/")
    assert r.status_code == 200
    assert "horst" in r.text
    assert "Dashboard" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_dashboard.py -v`
Expected: 404 on `/admin/`.

- [ ] **Step 3: Implement `dashboard_router.py`**

Create `src/localmail/serve/admin/dashboard_router.py`:

```python
"""GET /admin/ — authenticated dashboard."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from localmail.api.admin.auth import AdminUser
from localmail.api.admin.csrf import make_csrf_token
from localmail.serve.admin.dependencies import require_admin_session

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def get_dashboard(
    request: Request,
    admin: AdminUser = require_admin_session(),
) -> HTMLResponse:
    s_key = request.app.state.serve_config.session_signing_key.encode("latin1")
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "current_user": admin,
            "flashes": [],
            "csrf_token_for": lambda action: make_csrf_token(
                user_id=admin.id, action=action, key=s_key,
            ),
        },
    )
```

- [ ] **Step 4: Write `dashboard.html`**

Create `src/localmail/serve/admin/templates/dashboard.html`:

```html
{% extends "base.html" %}
{% block title %}Dashboard — localmail admin{% endblock %}
{% block content %}
<h1>Dashboard</h1>
<p>Signed in as <strong>{{ current_user.username }}</strong>.</p>
<div class="admin-card">
  <p>Admin actions will appear here once the rest of Sub-plans 2–6 ship:</p>
  <ul>
    <li>Account management — Sub-plan 3</li>
    <li>Daemon control — Sub-plan 5</li>
    <li>Mbox imports — Sub-plan 6</li>
  </ul>
</div>
{% endblock %}
```

- [ ] **Step 5: Note the dependency on Task 17**

Cannot pass until the dashboard router is registered. Proceed.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/admin/dashboard_router.py \
        src/localmail/serve/admin/templates/dashboard.html \
        tests/test_serve_admin_dashboard.py
git commit -m "feat(admin): GET /admin/ dashboard placeholder (test pending Task 17)"
```

---

## Task 16: Update `base.html` to use `csrf_token_for` helper

**Why:** `base.html` references `csrf_token_for("htmx")` and `csrf_token_for('/admin/logout')`, which the dashboard router passes in via the template context. Login pages don't have an authenticated user, so they pass `current_user=None` and the `{% if current_user %}` blocks suppress those calls. No code change needed if Task 10's `base.html` is already correct — verify.

**Files:**
- Verify: `src/localmail/serve/admin/templates/base.html`

- [ ] **Step 1: Re-read `base.html` and confirm:**

  - Lines that call `csrf_token_for(...)` are inside `{% if current_user %}` guards.
  - The logout form's hidden `csrf_token` field uses `{{ csrf_token_for('/admin/logout') }}`.
  - The `<body>` tag's `hx-headers` uses `{{ csrf_token_for("htmx") }}` and is only emitted when authenticated.

If anything diverges from Task 10's template, fix it. Otherwise no commit needed for this task.

- [ ] **Step 2: No-op confirmation**

Run: `grep -n "csrf_token_for" src/localmail/serve/admin/templates/base.html`
Expected: at least two matches, both inside the `{% if current_user %}` block.

---

## Task 17: Wire admin router + static + middleware into `create_app`

**Why:** All the pieces above are mountable. This task connects them and runs the end-to-end smoke test that all the deferred tests have been waiting for.

**Files:**
- Modify: `src/localmail/serve/app.py`

- [ ] **Step 1: Read the current `create_app`**

Run: `grep -n "include_router" src/localmail/serve/app.py`

You should already have a list of existing `app.include_router(...)` lines.

- [ ] **Step 2: Add imports and mount logic**

Open `src/localmail/serve/app.py`. Add to the existing imports:

```python
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from localmail.serve.admin import auth_router as admin_auth_router
from localmail.serve.admin import dashboard_router as admin_dashboard_router
from localmail.serve.admin.dependencies import install_admin_redirect_handler
from localmail.serve.admin.middleware import ScrubSensitiveQueryParamsMiddleware
```

Inside `create_app`, **after** the existing middleware adds and **before** the `app.include_router(version_routes.router, prefix="/v1")` line, add:

```python
    # Admin UI: only mount if signing keys are configured. Empty keys mean
    # the operator hasn't opted in; we still build the rest of the app.
    if cfg.session_signing_key:
        if not cfg.state_signing_key:
            raise RuntimeError(
                "ServeConfig.state_signing_key is empty while session_signing_key is "
                "set; admin UI requires both. See "
                "docs/superpowers/specs/2026-05-28-admin-ui-design.md §3."
            )
        app.add_middleware(
            ScrubSensitiveQueryParamsMiddleware,
            sensitive=("code", "state", "password"),
        )
        install_admin_redirect_handler(app)
        app.include_router(admin_auth_router.router, prefix="/admin")
        app.include_router(admin_dashboard_router.router, prefix="/admin")
        admin_static = Path(__file__).parent / "admin" / "static"
        app.mount(
            "/admin/static",
            StaticFiles(directory=str(admin_static)),
            name="admin_static",
        )
```

- [ ] **Step 3: Adjust the TestClient cookie behaviour for cross-test routes**

The login test issued a cookie with `secure=True; samesite=lax`. The TestClient by default uses `http://testserver` which strips `Secure` cookies. The cleanest fix is **conditional Secure**: in `auth_router.post_login` and `post_logout`, replace the literal `secure=True` with a check on the request scheme.

In `src/localmail/serve/admin/auth_router.py`, find both occurrences of `secure=True,` and replace with:

```python
        secure=(request.url.scheme == "https"),
```

(Comments are unnecessary; the reason — TestClient runs on http — is obvious to anyone reading.)

- [ ] **Step 4: Run all deferred tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_login.py tests/test_serve_admin_logout.py tests/test_serve_admin_dashboard.py -v`
Expected: 7 + 1 + 2 = 10 passed.

If any fail, fix-don't-skip: read the failure, decide whether the test or the implementation needs correcting, fix, re-run.

- [ ] **Step 5: Run full test suite to confirm no regressions**

Run: `unset VIRTUAL_ENV && uv run pytest`
Expected: no new failures vs. main. Pre-existing skips (DB-unreachable, slow integration) are fine.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/app.py src/localmail/serve/admin/auth_router.py
git commit -m "feat(admin): wire admin router + static + scrubber into create_app"
```

---

## Task 18: End-to-end smoke documented in `gui-admin/SMOKE.md`

**Why:** Manual verification step an operator can follow after a fresh install. Mirrors the `gui/README.md` "Manual smoke" sections.

**Files:**
- Create: `docs/operations/admin-ui-smoke.md`

- [ ] **Step 1: Write the smoke doc**

Create `docs/operations/admin-ui-smoke.md`:

```markdown
# Admin UI — manual smoke (Sub-plan 1)

After applying migrations and configuring `[serve] session_signing_key` and
`[serve] state_signing_key` in `config.toml`, verify the admin UI scaffolding
end-to-end.

## Prerequisites

```bash
# Generate two distinct signing keys
python -c "import secrets; print(secrets.token_urlsafe(32))"   # session_signing_key
python -c "import secrets; print(secrets.token_urlsafe(32))"   # state_signing_key
```

Add to `~/.config/localmail/config.toml`:

```toml
[serve]
session_signing_key = "<paste first key>"
state_signing_key   = "<paste second key>"
oauth_callback_url  = "https://localhost:8443/admin/oauth/callback"
```

## Bootstrap the first admin

```bash
uv run localmail init-db                           # apply migrations 0021
uv run localmail add-api-user --admin horst        # interactive password prompt
uv run localmail list-api-users --with-grants      # confirm is_admin = TRUE
```

## Sign in

```bash
uv run localmail serve --bind 127.0.0.1 --port 8443
```

In your browser:

1. Visit `https://127.0.0.1:8443/admin/` — you should be redirected to `/admin/login`.
2. Sign in with `horst` / your password — you land on the dashboard at `/admin/`.
3. The dashboard shows "Signed in as **horst**" and three placeholder bullet points.
4. Click "Sign out" — you return to `/admin/login` and the session cookie is gone
   (`document.cookie` empty in DevTools).
5. Try to visit `/admin/` again — you are redirected to `/admin/login`.

If any of those fail, that's a Sub-plan 1 regression — investigate before
starting Sub-plan 2.

## Negative cases worth checking

- Tampered cookie: edit `localmail_admin_session` in DevTools → flip one
  character → reload `/admin/` → redirects to login.
- Non-admin user: `uv run localmail revoke-admin horst`, then try to sign
  in → form re-renders with "This account is not an admin."
- Missing signing key: remove `session_signing_key` from `config.toml`,
  restart `serve` → `/admin/login` returns a 404 (admin routes not mounted).
```

- [ ] **Step 2: Commit**

```bash
git add docs/operations/admin-ui-smoke.md
git commit -m "docs(admin): manual smoke for Sub-plan 1 (auth scaffolding)"
```

---

## Sub-plan 1 — completion checklist

Run this exact command to ensure every new test passes:

```bash
unset VIRTUAL_ENV && uv run pytest \
  tests/test_migration_0021.py \
  tests/test_admin_session_tokens.py \
  tests/test_admin_csrf.py \
  tests/test_admin_auth_service.py \
  tests/test_admin_auth_cli.py \
  tests/test_serve_admin_dependencies.py \
  tests/test_serve_admin_middleware.py \
  tests/test_serve_admin_login.py \
  tests/test_serve_admin_logout.py \
  tests/test_serve_admin_dashboard.py \
  -v
```

Expected: all green. If the DB tests skip (no Postgres reachable), spin up
`localmail_test` first via the same setup the rest of the test suite uses.

Then run the whole suite for regressions:

```bash
unset VIRTUAL_ENV && uv run pytest -q
```

Expected: no new failures vs. the pre-sub-plan baseline.

When complete, open the next sub-plan brainstorm: **Sub-plan 2 — DB-canonical
accounts** (migration 0020, daemon/CLI refactor, init-db TOML→DB merge). The
spec section to re-read first is [§1 Schema additions
→ 0020](../specs/2026-05-28-admin-ui-design.md#0020_accounts_canonicalsql)
and [§2A Account management — Service layer](../specs/2026-05-28-admin-ui-design.md#service-layer).

---

## Future sub-plans (roadmap, not yet planned)

These follow Sub-plan 1; each gets its own dedicated plan-writing session
when we're ready to execute it.

- **Sub-plan 2 — DB-canonical accounts.** Migration 0020. Refactor
  `daemon`, `cli`, `sync` to read accounts from DB instead of `cfg.accounts`.
  `init-db` TOML→DB merge. End state: existing operators upgrade
  transparently; no user-visible behaviour change yet.
- **Sub-plan 3 — Account management UI.** Service layer
  (`api/admin/accounts.py`), HTTP routes (`/v1/admin/accounts`), UI for
  password-auth accounts, test-connection endpoint. End state: operator
  adds a password IMAP account end-to-end through the browser.
- **Sub-plan 4 — Gmail OAuth via web.** HMAC state token (uses Task 5's
  `state_signing_key`), `/admin/oauth/callback`, UI for OAuth account
  creation. End state: operator adds a Gmail OAuth account through the
  browser.
- **Sub-plan 5 — Daemon control.** Migration 0023 (`daemon_heartbeats`),
  per-account thread heartbeat writes, `DaemonSupervisor`, Unix socket
  for CLI parity, HTTP routes, UI panel. End state: start/stop/restart
  from UI + CLI.
- **Sub-plan 6 — Mbox import.** Migration 0022 (`import_jobs`),
  `localmail.import_worker` module, `ImportWorkerSupervisor`, upload +
  server-path delivery, UI for job submission/monitoring, `localmail
  import-mbox` CLI. End state: operator imports an mbox file end-to-end.
