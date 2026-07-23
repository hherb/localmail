# Admin Mode GUI — Phase 1 (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a native (bearer-token) admin client drive the existing `/v1/admin/*` JSON API — by exposing `is_admin` on the auth surface and adding a bearer-or-cookie admin dependency that skips CSRF for bearer auth — without changing the web HTMX admin.

**Architecture:** Two additive backend changes. (1) `AuthenticatedUser` and `GET /v1/auth/whoami` gain `is_admin` so the app knows when to reveal admin nav. (2) A new `require_admin` dependency accepts `Authorization: Bearer <admin token>` **or** the existing admin session cookie; `check_csrf` becomes a no-op for bearer requests (a bearer header carries no ambient cookie credential, so CSRF does not apply). The dependency is swapped into the four `/v1/admin/*` JSON routers; the `*_panel_router.py` HTML routers keep cookie + CSRF unchanged.

**Tech Stack:** Python 3.12, FastAPI, psycopg v3 (raw SQL, no ORM), pytest, mypy.

## Global Constraints

- Python ≥ 3.12, managed by `uv`. Run tests with `unset VIRTUAL_ENV && uv run pytest …`.
- Postgres via psycopg v3 + raw SQL. **No ORM.** **No new migration** — `api_users.is_admin` already exists.
- **No `cur.fetchone()[0]` without `assert row is not None` first** — mypy is enabled and will flag it.
- **No comments unless the WHY is non-obvious.** Don't restate SQL/Python.
- DB tests TRUNCATE before each test (the `db_conn` fixture). Never `DROP TABLE`. `LOCALMAIL_TEST_DSN` defaults to the `localmail_test` DB.
- The web HTMX admin's cookie + CSRF behaviour MUST be unchanged. The `tests/test_session_cookie_scope.py` invariant MUST stay green.
- SPDX header on every new source/test file: `# SPDX-License-Identifier: AGPL-3.0-or-later` then `# Copyright (C) 2026 Horst Herb`.
- Full type check after the last task: `unset VIRTUAL_ENV && uv run mypy src/localmail` → clean.

---

### Task 1: `is_admin` on `AuthenticatedUser` + `verify_token`

**Files:**
- Modify: `src/localmail/api/auth.py` (the `AuthenticatedUser` dataclass ~line 292; `verify_token` ~lines 337–365)
- Test: `tests/test_api_auth_tokens.py` (append)

**Interfaces:**
- Produces: `AuthenticatedUser(id: int, username: str, is_admin: bool = False)` — frozen dataclass with a **defaulted** `is_admin` so existing 2-arg constructions still type-check. `verify_token(conn, token) -> AuthenticatedUser | None` now populates `is_admin` from `api_users.is_admin`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_auth_tokens.py`:

```python
def test_verify_token_reports_is_admin_flag(db_conn):
    from localmail.api.auth import hash_password, issue_token, verify_token

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE), ('peon', %s, FALSE) RETURNING id",
            (hash_password("pw"), hash_password("pw")),
        )
    with db_conn.cursor() as cur:
        cur.execute("SELECT id, username FROM api_users ORDER BY username")
        rows = cur.fetchall()
    ids = {username: uid for uid, username in rows}
    admin_tok, _ = issue_token(db_conn, ids["root"])
    peon_tok, _ = issue_token(db_conn, ids["peon"])
    db_conn.commit()

    admin_user = verify_token(db_conn, admin_tok)
    peon_user = verify_token(db_conn, peon_tok)
    assert admin_user is not None and admin_user.is_admin is True
    assert peon_user is not None and peon_user.is_admin is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_tokens.py::test_verify_token_reports_is_admin_flag -v`
Expected: FAIL — `AuthenticatedUser` has no `is_admin` (TypeError) or the attribute is missing.

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/api/auth.py`, add the field to the dataclass:

```python
@dataclass(frozen=True)
class AuthenticatedUser:
    """The user behind a valid bearer token."""
    id: int
    username: str
    is_admin: bool = False
```

In `verify_token`, add `u.is_admin` to the SELECT and to the return:

```python
        cur.execute(
            "SELECT u.id, u.username, u.is_admin "
            "FROM api_tokens t "
            "JOIN api_users u ON u.id = t.user_id "
            "WHERE t.token_sha256 = %s "
            "  AND t.expires_at > now() "
            "  AND u.disabled_at IS NULL",
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
    return AuthenticatedUser(id=row[0], username=row[1], is_admin=bool(row[2]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_tokens.py::test_verify_token_reports_is_admin_flag -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/auth.py tests/test_api_auth_tokens.py
git commit -m "feat(auth): expose is_admin on AuthenticatedUser + verify_token"
```

---

### Task 2: `is_admin` on `GET /v1/auth/whoami`

**Files:**
- Modify: `src/localmail/serve/routes/auth.py` (`WhoamiResponse` model + `whoami` handler)
- Test: `tests/test_serve_auth_routes.py` (append)

**Interfaces:**
- Consumes: `AuthenticatedUser.is_admin` (Task 1).
- Produces: `WhoamiResponse(username: str, user_id: str, is_admin: bool)` — the app reads `is_admin` after login to decide whether to show admin nav.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_serve_auth_routes.py`:

```python
def test_whoami_reports_is_admin(db_dsn, db_conn):
    from fastapi.testclient import TestClient
    from localmail.api.auth import hash_password, issue_token
    from localmail.serve.app import create_app

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE), ('peon', %s, FALSE) RETURNING id",
            (hash_password("pw"), hash_password("pw")),
        )
        cur.execute("SELECT username, id FROM api_users")
        ids = {u: i for u, i in cur.fetchall()}
    admin_tok, _ = issue_token(db_conn, ids["root"])
    peon_tok, _ = issue_token(db_conn, ids["peon"])
    db_conn.commit()

    client = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r_admin = client.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {admin_tok}"})
    r_peon = client.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {peon_tok}"})
    assert r_admin.status_code == 200 and r_admin.json()["is_admin"] is True
    assert r_peon.status_code == 200 and r_peon.json()["is_admin"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_auth_routes.py::test_whoami_reports_is_admin -v`
Expected: FAIL — `KeyError: 'is_admin'` (field absent from the response).

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/serve/routes/auth.py`, add the field and populate it:

```python
class WhoamiResponse(BaseModel):
    username: str
    user_id: str
    is_admin: bool


@router.get("/whoami", response_model=WhoamiResponse)
def whoami(user=Depends(get_authenticated_user)) -> WhoamiResponse:
    return WhoamiResponse(
        username=user.username, user_id=str(user.id), is_admin=user.is_admin
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_auth_routes.py::test_whoami_reports_is_admin -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/routes/auth.py tests/test_serve_auth_routes.py
git commit -m "feat(auth): expose is_admin on GET /v1/auth/whoami"
```

---

### Task 3: `require_admin` dependency + conditional CSRF, wired into accounts_router

**Files:**
- Modify: `src/localmail/serve/admin/dependencies.py` (extract cookie logic, add `require_admin`)
- Modify: `src/localmail/serve/admin/csrf.py` (`check_csrf` skips bearer)
- Modify: `src/localmail/serve/admin/accounts_router.py` (import + swap `require_admin_session()` → `require_admin()`)
- Test: `tests/test_serve_admin_bearer_auth.py` (new)

**Interfaces:**
- Consumes: `AuthenticatedUser.is_admin` (Task 1); `verify_token`; `AdminUser`; `InvalidToken`.
- Produces: `require_admin() -> Depends[...]` usable exactly like `require_admin_session()` as a default (`admin: AdminUser = require_admin()`). Bearer path sets `request.state.admin_auth_kind = "bearer"` and returns `AdminUser`; cookie path sets `"cookie"`. `check_csrf(request, admin, csrf_token, action)` returns immediately when `request.state.admin_auth_kind == "bearer"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_admin_bearer_auth.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bearer-token admin auth on /v1/admin/* (native client path).

A bearer token for an is_admin user drives the admin JSON API with no
CSRF; a non-admin token is 403; a bad token is 401. The cookie web-admin
path still requires and verifies CSRF (regression).
"""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password, issue_token
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
def client(app):
    return TestClient(app, follow_redirects=False)


def _make_user(conn: psycopg.Connection, username: str, *, is_admin: bool) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, %s) RETURNING id",
            (username, hash_password("pw"), is_admin),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def admin_token(db_conn):
    uid = _make_user(db_conn, "root", is_admin=True)
    tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    return tok


@pytest.fixture
def user_token(db_conn):
    uid = _make_user(db_conn, "peon", is_admin=False)
    tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    return tok


@pytest.fixture
def cookie_client(app, db_conn):
    uid = _make_user(db_conn, "webadmin", is_admin=True)
    db_conn.commit()
    c = TestClient(app, follow_redirects=False)
    form = c.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    r = c.post(
        "/admin/login",
        data={"username": "webadmin", "password": "pw", "csrf_token": m.group(1)},
    )
    assert r.status_code == 303, r.text

    def csrf_for(action: str, method: str = "POST") -> str:
        return make_csrf_token(
            user_id=uid, action=csrf_action(method, action),
            key=_SIGNING_KEY.encode("ascii"),
        )

    c.csrf_for = csrf_for  # type: ignore[attr-defined]
    return c


def _create_body(name: str) -> dict:
    return {"name": name, "email_address": f"{name}@x.test", "auth_method": "archive"}


def test_bearer_admin_lists_accounts(client, admin_token):
    r = client.get("/v1/admin/accounts", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text


def test_bearer_admin_creates_account_without_csrf(client, admin_token):
    r = client.post(
        "/v1/admin/accounts",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=_create_body("work"),
    )
    assert r.status_code in (200, 201), r.text


def test_non_admin_bearer_forbidden(client, user_token):
    r = client.get("/v1/admin/accounts", headers={"Authorization": f"Bearer {user_token}"})
    assert r.status_code == 403


def test_bad_bearer_unauthorized(client):
    r = client.get("/v1/admin/accounts", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_cookie_admin_mutation_without_csrf_still_400(cookie_client):
    r = cookie_client.post("/v1/admin/accounts", json=_create_body("z"))
    assert r.status_code == 400


def test_cookie_admin_mutation_with_csrf_succeeds(cookie_client):
    r = cookie_client.post(
        "/v1/admin/accounts",
        headers={"X-CSRF-Token": cookie_client.csrf_for("/v1/admin/accounts")},
        json=_create_body("z2"),
    )
    assert r.status_code in (200, 201), r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_bearer_auth.py -v`
Expected: FAIL — the bearer tests get 303 (redirect to `/admin/login`, since `require_admin_session` only reads the cookie), not 200/403/401.

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/serve/admin/dependencies.py`, replace the `require_admin_session` block with an extracted cookie helper plus the new dependency. Add imports at the top: `from localmail.api.auth import verify_token` and `from localmail.api.errors import InvalidToken`.

```python
def _admin_from_cookie(request: Request) -> AdminUser:
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
            return get_admin_user(
                conn, user_id=payload.user_id, issued_at=payload.issued_at
            )
        except UserNotFound:
            raise _AdminRedirect()
        except SessionInvalidated:
            raise _AdminRedirect()
        except NotAnAdmin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="not an admin"
            )


def require_admin_session():
    """Dependency factory; returns the AdminUser or raises redirect/403."""
    def _dep(request: Request) -> AdminUser:
        request.state.admin_auth_kind = "cookie"
        return _admin_from_cookie(request)
    return Depends(_dep)


def require_admin():
    """Admin via bearer token OR admin session cookie.

    Native clients send ``Authorization: Bearer <token>``; the user must be
    ``is_admin`` (else 403), a bad/expired token is 401. Sets
    ``request.state.admin_auth_kind = "bearer"`` so ``check_csrf`` skips CSRF —
    a bearer header carries no ambient cookie credential, so CSRF does not
    apply. With no bearer header the existing cookie path runs unchanged.
    """
    def _dep(request: Request) -> AdminUser:
        authz = request.headers.get("Authorization", "")
        if authz.startswith("Bearer "):
            token = authz[len("Bearer "):]
            pool = request.app.state.pool
            with pool.connection() as conn:
                user = verify_token(conn, token)
                conn.commit()
            if user is None:
                raise InvalidToken("token is invalid, expired, or revoked")
            if not user.is_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="not an admin"
                )
            request.state.admin_auth_kind = "bearer"
            return AdminUser(id=user.id, username=user.username)
        request.state.admin_auth_kind = "cookie"
        return _admin_from_cookie(request)
    return Depends(_dep)
```

In `src/localmail/serve/admin/csrf.py`, make `check_csrf` skip bearer at the top of the function body (before the `if not csrf_token` check):

```python
def check_csrf(
    request: Request,
    admin: AdminUser,
    csrf_token: str,
    action: str,
) -> None:
    """Raise HTTPException(400) if the CSRF token is missing or invalid.

    Bearer-authenticated admin requests (native clients) carry no ambient
    cookie credential, so CSRF does not apply and is skipped.
    """
    if getattr(request.state, "admin_auth_kind", "cookie") == "bearer":
        return
    if not csrf_token:
        raise HTTPException(status_code=400, detail="CSRF token missing")
    key = session_signing_key(request)
    bound = csrf_action(request.method, action)
    try:
        verify_csrf_token(csrf_token, user_id=admin.id, action=bound, key=key)
    except CSRFError:
        raise HTTPException(status_code=400, detail="CSRF token invalid")
```

In `src/localmail/serve/admin/accounts_router.py`, change the import and every dependency default:

```python
from localmail.serve.admin.dependencies import require_admin
```

Then replace each `admin: AdminUser = require_admin_session()` with `admin: AdminUser = require_admin()` (7 occurrences).

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_bearer_auth.py -v`
Expected: PASS (all 6)

Then confirm no regression on the existing accounts routes and CSRF tests:

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_csrf.py tests/test_serve_accounts_routes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/dependencies.py src/localmail/serve/admin/csrf.py \
        src/localmail/serve/admin/accounts_router.py tests/test_serve_admin_bearer_auth.py
git commit -m "feat(admin): bearer-or-cookie require_admin + CSRF-skip-for-bearer (accounts_router)"
```

---

### Task 4: Wire `require_admin` into users / imports / daemon routers + regression sweep

**Files:**
- Modify: `src/localmail/serve/admin/users_router.py` (import + swap; 8 occurrences)
- Modify: `src/localmail/serve/admin/imports_router.py` (import + swap; 4 occurrences)
- Modify: `src/localmail/serve/admin/daemon_router.py` (import + swap; 6 occurrences)
- Test: `tests/test_serve_admin_bearer_auth.py` (append)

**Interfaces:**
- Consumes: `require_admin()` (Task 3). Same drop-in swap as accounts_router.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_serve_admin_bearer_auth.py`:

```python
@pytest.mark.parametrize("path", ["/v1/admin/users", "/v1/admin/imports", "/v1/admin/daemon"])
def test_bearer_admin_reads_every_admin_router(client, admin_token, path):
    r = client.get(path, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", ["/v1/admin/users", "/v1/admin/imports", "/v1/admin/daemon"])
def test_non_admin_bearer_forbidden_on_every_router(client, user_token, path):
    r = client.get(path, headers={"Authorization": f"Bearer {user_token}"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_bearer_auth.py -k every_admin_router -v`
Expected: FAIL — users/imports/daemon still use `require_admin_session`, so bearer gets 303 not 200.

- [ ] **Step 3: Write minimal implementation**

In each of `users_router.py`, `imports_router.py`, `daemon_router.py`:
- change `from localmail.serve.admin.dependencies import require_admin_session` → `from localmail.serve.admin.dependencies import require_admin`
- replace every `require_admin_session()` with `require_admin()`.

Verify no stray references remain:

Run: `grep -rn "require_admin_session" src/localmail/serve/admin/*_router.py`
Expected: no output (the `*_panel_router.py` HTML routers are untouched and keep `require_admin_session`).

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_bearer_auth.py -v`
Expected: PASS

Regression sweep — the cookie-scope invariant and the existing admin route/panel suites:

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_session_cookie_scope.py tests/test_serve_daemon_routes.py tests/test_serve_admin_imports.py tests/test_api_admin_users.py tests/test_serve_daemon_panel.py -v`
Expected: PASS

Full type check:

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean (no new errors introduced by these changes)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/users_router.py \
        src/localmail/serve/admin/imports_router.py \
        src/localmail/serve/admin/daemon_router.py \
        tests/test_serve_admin_bearer_auth.py
git commit -m "feat(admin): bearer-or-cookie require_admin across users/imports/daemon routers"
```

---

## After Phase 1

Phase 1 is independently shippable (open a PR from `feat/admin-mode-gui`). Subsequent phases each get their own plan when we reach them, per the design's phasing:

- **Phase 2** — frontend shell: `whoami.is_admin` detection, admin nav/route, `AdminView` scaffold (`gui/`).
- **Phase 3** — Accounts panel (+ Gmail OAuth connect).
- **Phase 4** — Daemon panel (status/logs + DB-queue controls; lifecycle gated on `supervise_daemon_externally`).
- **Phase 5** — Users & ACL panel.
- **Phase 6** — Imports panel.
- **Phase 7** — macOS packaging note (dmg already configured; codesign/notarize is a separate ops task).

Design reference: [docs/superpowers/specs/2026-07-23-admin-mode-tauri-gui-design.md](../specs/2026-07-23-admin-mode-tauri-gui-design.md).

## Self-Review

- **Spec coverage (backend section):** `is_admin` on whoami → Task 2; `AuthenticatedUser.is_admin` → Task 1; `require_admin` bearer-or-cookie + 403 non-admin + conditional CSRF → Task 3; swap into all four routers → Tasks 3–4; cookie path + CSRF regression + cookie-scope invariant → Tasks 3–4. Frontend/Rust sections are explicitly deferred to later plans (Phases 2–6).
- **Placeholder scan:** none — every step carries runnable code/commands.
- **Type consistency:** `require_admin()` mirrors `require_admin_session()`'s `Depends`-returning shape and both yield `AdminUser`; `admin_auth_kind` string values (`"bearer"`/`"cookie"`) are set in `dependencies.py` and read in `csrf.py` identically; `AuthenticatedUser.is_admin` defined in Task 1 is consumed in Tasks 2–3.
