# Account CRUD Admin Screens (2A.3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `/admin/accounts` HTML screens (list, create, edit, delete, password, test-connection, enable/disable, Gmail OAuth) that drive the existing `api/admin/accounts` service, and wire `probe_connection` to work for oauth2 accounts.

**Architecture:** Server-rendered HTMX partials. A thin `accounts_panel_router.py` mounted at `/admin` renders Jinja fragments and dispatches to the `api/admin/accounts` service; all form logic lives in the pure, unit-tested `account_forms.py`. The `/v1/admin/accounts` JSON routes stay untouched for machine clients. Method-bound CSRF (`csrf_token_for_method` + `check_csrf`) closes #125. All JS is served-static (`script-src 'self'`).

**Tech Stack:** Python 3.12, FastAPI, Jinja2, HTMX, psycopg v3, pytest, `uv`.

**Spec:** [docs/superpowers/specs/2026-06-04-account-admin-screens-design.md](../specs/2026-06-04-account-admin-screens-design.md)

**Conventions reminder:** Run everything as `unset VIRTUAL_ENV && uv run …`. TRUNCATE-based DB fixtures (`db_conn`, `db_dsn`). `memory_keyring` is autouse. No magic numbers/strings (flag set is a module constant). Files under 500 lines. No comments unless the WHY is non-obvious.

---

## File Structure

```
NEW  src/localmail/serve/admin/account_forms.py            # PURE form logic, no IO
NEW  src/localmail/serve/admin/accounts_panel_router.py    # thin HTML routes
NEW  src/localmail/serve/admin/templates/accounts/list.html
NEW  src/localmail/serve/admin/templates/accounts/_row.html
NEW  src/localmail/serve/admin/templates/accounts/form.html
NEW  src/localmail/serve/admin/templates/accounts/_form_fields.html
NEW  src/localmail/serve/admin/templates/accounts/_test_result.html
NEW  src/localmail/serve/admin/templates/accounts/_secret_status.html
NEW  src/localmail/serve/admin/static/accounts-panel.js
NEW  tests/test_account_forms.py
NEW  tests/test_serve_admin_account_screens.py
EDIT src/localmail/api/admin/accounts.py                   # oauth2 probe wiring
EDIT src/localmail/serve/admin/accounts_router.py          # JSON test-connection passes gmail secrets
EDIT src/localmail/serve/app.py                            # register accounts_panel_router
EDIT tests/test_admin_accounts.py                          # probe_connection oauth2 unit tests
```

---

## Task 1: Wire oauth2 into `probe_connection`

**Files:**
- Modify: `src/localmail/api/admin/accounts.py` (`_open_imap_connection`, `probe_connection`)
- Test: `tests/test_admin_accounts.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_admin_accounts.py`. First check the file's existing imports/fixtures (it already exercises `accounts.py` against `db_conn`); reuse its account-creation helper if present, otherwise insert rows directly as below.

```python
from pathlib import Path
from contextlib import contextmanager

from localmail.api.admin import accounts as svc


def _make_oauth_account(db_conn) -> int:
    acct = svc.create_account(
        db_conn,
        name="g", email_address="g@gmail.com", auth_method="oauth2",
        imap_host="imap.gmail.com", imap_port=993, oauth_provider="gmail",
        folder_allow=None, folder_deny=None, folder_deny_flags=None,
    )
    return acct.id


class _FakeClient:
    def list_folders(self):
        return [((rb"\HasNoChildren",), b"/", "INBOX"),
                ((rb"\Junk",), b"/", "[Gmail]/Spam")]


def test_probe_connection_oauth2_lists_folders(db_conn, monkeypatch):
    aid = _make_oauth_account(db_conn)

    @contextmanager
    def fake_open(account, *, gmail_client_secrets=None):
        assert account.auth_method == "oauth2"
        assert gmail_client_secrets == Path("/tmp/secrets.json")
        yield _FakeClient()

    monkeypatch.setattr(svc, "_open_imap_connection", fake_open)
    folders = svc.probe_connection(
        db_conn, aid, gmail_client_secrets=Path("/tmp/secrets.json")
    )
    names = [f.name for f in folders]
    assert names == ["INBOX", "[Gmail]/Spam"]


def test_probe_connection_oauth2_missing_token_is_field_error(db_conn, monkeypatch):
    aid = _make_oauth_account(db_conn)

    @contextmanager
    def fake_open(account, *, gmail_client_secrets=None):
        raise RuntimeError("no OAuth refresh token stored for 'g'; run oauth-login")
        yield  # pragma: no cover

    monkeypatch.setattr(svc, "_open_imap_connection", fake_open)
    with pytest.raises(svc.AccountFieldError):
        svc.probe_connection(
            db_conn, aid, gmail_client_secrets=Path("/tmp/secrets.json")
        )


def test_probe_connection_archive_still_refused(db_conn):
    acct = svc.create_account(
        db_conn, name="arch", email_address="a@x.org", auth_method="archive",
        imap_host=None, imap_port=None, oauth_provider=None,
        folder_allow=None, folder_deny=None, folder_deny_flags=None,
    )
    with pytest.raises(svc.AccountFieldError):
        svc.probe_connection(db_conn, acct.id)
```

Ensure `import pytest` and `import io`/`Path` exist at the top of the file (add if missing).

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py -k probe_connection -v`
Expected: FAIL — `test_probe_connection_oauth2_lists_folders` raises `AccountFieldError` ("not yet supported for oauth2"), and the missing-token test fails for the same wrong reason.

- [ ] **Step 3: Implement the wiring**

In `src/localmail/api/admin/accounts.py`, change `_open_imap_connection` to accept and thread the secrets path:

```python
def _open_imap_connection(
    account: Account, *, gmail_client_secrets: Path | None = None
) -> AbstractContextManager[IMAPClient]:
    """Indirection point so tests can monkeypatch without touching real IMAP."""
    cfg = _AccountConfig(
        name=account.name,
        email=account.email_address,
        imap_host=account.imap_host or '',
        imap_port=account.imap_port or 993,
        auth_method=account.auth_method,  # type: ignore[arg-type]
        oauth_provider=account.oauth_provider,  # type: ignore[arg-type]
    )
    return _imap.open_connection(cfg, gmail_client_secrets=gmail_client_secrets)
```

Add `from pathlib import Path` to the imports if not present. Then replace `probe_connection`:

```python
def probe_connection(
    conn: psycopg.Connection,
    account_id: int,
    *,
    gmail_client_secrets: Path | None = None,
) -> list[FolderInfo]:
    """Open IMAP, list folders, return summary. Raises on connect failure.

    Archive accounts raise AccountFieldError (no host to probe). oauth2 accounts
    require ``gmail_client_secrets`` (threaded in by the caller from
    app.state.gmail_client_secrets_file); a missing refresh token surfaces as a
    clean AccountFieldError rather than an opaque 500.
    """
    account = get_account(conn, account_id)
    if account.auth_method == 'archive':
        raise AccountFieldError(
            "probe_connection not applicable to archive accounts"
        )
    if account.auth_method == 'oauth2' and gmail_client_secrets is None:
        raise AccountFieldError(
            "Gmail OAuth is not configured on this server "
            "([gmail_oauth] client_secrets_file)"
        )
    try:
        with _open_imap_connection(
            account, gmail_client_secrets=gmail_client_secrets
        ) as client:
            listing = client.list_folders()
    except RuntimeError as e:
        raise AccountFieldError(str(e)) from e
    return [FolderInfo(name=name, flags=tuple(flags)) for flags, _delim, name in listing]
```

The `except RuntimeError` maps `imap_client.open_connection`'s "no OAuth refresh token stored" / "no password stored" `RuntimeError`s to a field error. (Genuine connect failures — socket errors, auth rejection — still propagate as their own exception types for the route to surface as a 5xx/connection error fragment.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py -k probe_connection -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Update the JSON test-connection route to pass Gmail secrets**

In `src/localmail/serve/admin/accounts_router.py`, in `test_connection`, pass the secrets path from app state:

```python
    secrets_path = getattr(request.app.state, "gmail_client_secrets_file", None)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            folders = svc.probe_connection(
                conn, aid, gmail_client_secrets=secrets_path
            )
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
```

- [ ] **Step 6: Run the broader account suites to confirm no regression**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py tests/test_serve_admin_accounts.py -q`
Expected: PASS (existing tests still green; the JSON route change is signature-compatible since the new kwarg defaults to None).

- [ ] **Step 7: Commit**

```bash
git add src/localmail/api/admin/accounts.py src/localmail/serve/admin/accounts_router.py tests/test_admin_accounts.py
git commit -m "feat(admin): wire oauth2 into probe_connection (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Pure form helpers — `account_forms.py`

**Files:**
- Create: `src/localmail/serve/admin/account_forms.py`
- Test: `tests/test_account_forms.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_account_forms.py`:

```python
"""Unit tests for the pure account-form helpers (no IO)."""
from __future__ import annotations

import pytest

from localmail.api.admin.accounts import AccountFieldError
from localmail.serve.admin import account_forms as af


def test_deny_flags_constant_is_rfc6154_set():
    assert af.DENY_FLAGS == (
        r"\Trash", r"\Junk", r"\All", r"\Drafts",
        r"\Sent", r"\Important", r"\Flagged",
    )


@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("   \n  \n", None),
    ("INBOX", ["INBOX"]),
    ("INBOX\nLists/dev", ["INBOX", "Lists/dev"]),
    ("INBOX\r\nLists/dev\r\n", ["INBOX", "Lists/dev"]),
    ("  INBOX  \n\n  Spam ", ["INBOX", "Spam"]),
])
def test_parse_lines(raw, expected):
    assert af.parse_lines(raw) == expected


def test_parse_deny_flags_keeps_only_known():
    assert af.parse_deny_flags([r"\Trash", r"\Junk"]) == [r"\Trash", r"\Junk"]


def test_parse_deny_flags_empty_is_none():
    assert af.parse_deny_flags([]) is None


def test_parse_deny_flags_rejects_unknown():
    with pytest.raises(af.FormError):
        af.parse_deny_flags([r"\Trash", r"\Bogus"])


def test_form_to_create_kwargs_password():
    form = {
        "name": "fastmail", "email_address": "me@fastmail.com",
        "auth_method": "password", "imap_host": "imap.fastmail.com",
        "imap_port": "993", "oauth_provider": "",
        "folder_allow": "INBOX", "folder_deny": "", "deny_flags": [r"\Trash"],
    }
    kw = af.form_to_create_kwargs(form, deny_flags_selected=[r"\Trash"])
    assert kw == {
        "name": "fastmail", "email_address": "me@fastmail.com",
        "auth_method": "password", "imap_host": "imap.fastmail.com",
        "imap_port": 993, "oauth_provider": None,
        "folder_allow": ["INBOX"], "folder_deny": None,
        "folder_deny_flags": [r"\Trash"],
    }


def test_form_to_create_kwargs_bad_port_is_form_error():
    form = {
        "name": "x", "email_address": "x@x.com", "auth_method": "password",
        "imap_host": "h", "imap_port": "not-a-number", "oauth_provider": "",
        "folder_allow": "", "folder_deny": "",
    }
    with pytest.raises(af.FormError):
        af.form_to_create_kwargs(form, deny_flags_selected=[])


def test_form_to_create_kwargs_archive_nulls_host_port():
    form = {
        "name": "arch", "email_address": "a@x.org", "auth_method": "archive",
        "imap_host": "", "imap_port": "", "oauth_provider": "",
        "folder_allow": "", "folder_deny": "",
    }
    kw = af.form_to_create_kwargs(form, deny_flags_selected=[])
    assert kw["imap_host"] is None
    assert kw["imap_port"] is None
    assert kw["auth_method"] == "archive"


def test_account_to_form_values_roundtrips_lists():
    from datetime import datetime, timezone
    from localmail.api.admin.accounts import Account
    acct = Account(
        id=7, name="g", email_address="g@gmail.com", auth_method="oauth2",
        oauth_provider="gmail", imap_host=None, imap_port=None,
        folder_allow=["INBOX", "Lists/dev"], folder_deny=["Spam"],
        folder_deny_flags=[r"\Trash"], sync_enabled=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    vals = af.account_to_form_values(acct)
    assert vals["folder_allow"] == "INBOX\nLists/dev"
    assert vals["folder_deny"] == "Spam"
    assert vals["deny_flags_checked"] == {r"\Trash"}
    assert vals["auth_method"] == "oauth2"


def test_field_errors_from_maps_known_field():
    err = AccountFieldError("live accounts require imap_port in 1..65535")
    fe = af.field_errors_from(err)
    assert "imap_port" in fe


def test_field_errors_from_unknown_falls_back_to_form_level():
    err = AccountFieldError("some unmapped failure")
    fe = af.field_errors_from(err)
    assert fe == {"_form": "some unmapped failure"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_forms.py -v`
Expected: FAIL — `ModuleNotFoundError: localmail.serve.admin.account_forms`.

- [ ] **Step 3: Implement `account_forms.py`**

Create `src/localmail/serve/admin/account_forms.py`:

```python
"""Pure form-parsing helpers for the account admin screens (no IO).

The HTML router stays thin by delegating every raw-form → service-kwargs
transformation here, where it is unit-tested in isolation. The RFC 6154
special-use flag set is the single source of truth (no magic strings in
templates or the router).
"""
from __future__ import annotations

from localmail.api.admin.accounts import Account, AccountFieldError

# RFC 6154 IMAP special-use flags offered as folder-deny checkboxes. Closed
# set — folder_deny_flags must never contain anything outside this tuple.
DENY_FLAGS: tuple[str, ...] = (
    r"\Trash", r"\Junk", r"\All", r"\Drafts",
    r"\Sent", r"\Important", r"\Flagged",
)


class FormError(ValueError):
    """Raised for malformed raw form input the service layer wouldn't see
    (e.g. a non-numeric port, an unknown deny flag)."""


def parse_lines(text: str) -> list[str] | None:
    """Split a textarea into a stripped, blank-free list. Empty → None."""
    items = [line.strip() for line in text.splitlines()]
    items = [line for line in items if line]
    return items or None


def parse_deny_flags(selected: list[str]) -> list[str] | None:
    """Validate selected deny-flag checkboxes against DENY_FLAGS. Empty → None."""
    unknown = [f for f in selected if f not in DENY_FLAGS]
    if unknown:
        raise FormError(f"unknown deny flags: {unknown}")
    return list(selected) or None


def _parse_port(raw: str, *, auth_method: str) -> int | None:
    raw = raw.strip()
    if auth_method == "archive" or not raw:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise FormError("imap_port must be a number") from e


def _none_if_blank(value: str) -> str | None:
    value = value.strip()
    return value or None


def form_to_create_kwargs(form: dict, *, deny_flags_selected: list[str]) -> dict:
    """Map a raw create-form dict to create_account(**kwargs)."""
    auth_method = form["auth_method"]
    return {
        "name": form["name"].strip(),
        "email_address": form["email_address"].strip(),
        "auth_method": auth_method,
        "imap_host": (None if auth_method == "archive"
                      else _none_if_blank(form.get("imap_host", ""))),
        "imap_port": _parse_port(form.get("imap_port", ""), auth_method=auth_method),
        "oauth_provider": _none_if_blank(form.get("oauth_provider", "")),
        "folder_allow": parse_lines(form.get("folder_allow", "")),
        "folder_deny": parse_lines(form.get("folder_deny", "")),
        "folder_deny_flags": parse_deny_flags(deny_flags_selected),
    }


def form_to_patch_fields(form: dict, *, deny_flags_selected: list[str]) -> dict:
    """Map a raw edit-form dict to update_account(**fields).

    Name is immutable post-create (it keys the keyring secret), so it is not
    part of the patch.
    """
    fields = form_to_create_kwargs(form, deny_flags_selected=deny_flags_selected)
    fields.pop("name")
    return fields


def account_to_form_values(account: Account) -> dict:
    """Inverse of the create mapping, for prefilling the edit form."""
    return {
        "name": account.name,
        "email_address": account.email_address,
        "auth_method": account.auth_method,
        "oauth_provider": account.oauth_provider or "",
        "imap_host": account.imap_host or "",
        "imap_port": str(account.imap_port) if account.imap_port else "",
        "folder_allow": "\n".join(account.folder_allow or []),
        "folder_deny": "\n".join(account.folder_deny or []),
        "deny_flags_checked": set(account.folder_deny_flags or []),
        "sync_enabled": account.sync_enabled,
    }


# Substring → field-name map for surfacing a service AccountFieldError beside
# the offending input. Order matters: first match wins.
_FIELD_HINTS: tuple[tuple[str, str], ...] = (
    ("imap_port", "imap_port"),
    ("imap_host", "imap_host"),
    ("email_address", "email_address"),
    ("name", "name"),
    ("oauth_provider", "oauth_provider"),
    ("auth_method", "auth_method"),
)


def field_errors_from(err: AccountFieldError | FormError) -> dict[str, str]:
    """Map a validation error to {field: message}; falls back to a form-level
    error under the "_form" key when no field matches."""
    msg = str(err)
    for needle, field in _FIELD_HINTS:
        if needle in msg:
            return {field: msg}
    return {"_form": msg}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_forms.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Type-check**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail/serve/admin/account_forms.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/admin/account_forms.py tests/test_account_forms.py
git commit -m "feat(admin): pure account-form helpers (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: List page + router skeleton + registration

**Files:**
- Create: `src/localmail/serve/admin/accounts_panel_router.py`
- Create: `src/localmail/serve/admin/templates/accounts/list.html`
- Create: `src/localmail/serve/admin/templates/accounts/_row.html`
- Modify: `src/localmail/serve/app.py` (register router)
- Test: `tests/test_serve_admin_account_screens.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_serve_admin_account_screens.py` with the standard admin-session fixtures (copied from `test_serve_daemon_panel.py`) plus the list tests:

```python
"""Admin account-management HTML screens (2A.3)."""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin import accounts as svc
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY,
        state_signing_key="y" * 43,
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
            "VALUES (%s, %s, TRUE) RETURNING id",
            ("horst", pwh),
        )
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
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": m.group(1)},
    )
    assert r.status_code == 303, r.text
    return client


def _seed_account(db_conn, **over) -> int:
    kw = dict(
        name="fastmail", email_address="me@fastmail.com", auth_method="password",
        imap_host="imap.fastmail.com", imap_port=993, oauth_provider=None,
        folder_allow=None, folder_deny=None, folder_deny_flags=None,
    )
    kw.update(over)
    acct = svc.create_account(db_conn, **kw)
    db_conn.commit()
    return acct.id


def test_list_redirects_unauthenticated(app):
    client = TestClient(app, follow_redirects=False)
    r = client.get("/admin/accounts")
    assert r.status_code in (302, 303)
    assert "/admin/login" in r.headers["location"]


def test_list_renders_accounts(admin_client, db_conn):
    _seed_account(db_conn, name="fastmail")
    _seed_account(db_conn, name="work-gmail", email_address="me@company.com",
                  auth_method="oauth2", imap_host="imap.gmail.com",
                  oauth_provider="gmail")
    r = admin_client.get("/admin/accounts")
    assert r.status_code == 200
    assert "fastmail" in r.text
    assert "work-gmail" in r.text
    assert "New account" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -v`
Expected: FAIL — `/admin/accounts` 404s (route not registered).

- [ ] **Step 3: Create the router skeleton with the list route**

Create `src/localmail/serve/admin/accounts_panel_router.py`:

```python
"""Admin account-management HTML screens (2A.3).

Thin server-rendered HTMX router mounted at /admin. Renders Jinja fragments
and dispatches to the api/admin/accounts service; all form parsing lives in
account_forms. Mutating routes verify a method-bound CSRF token (X-CSRF-Token
header) via the shared check_csrf. JSON machine clients use /v1/admin/accounts.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from localmail.api.admin import accounts as svc
from localmail.api.admin.auth import AdminUser
from localmail.serve.admin.csrf import csrf_token_context, session_signing_key
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


@router.get("/accounts", response_class=HTMLResponse)
def list_accounts(
    request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_accounts(conn)
    ctx = _base_context(request, admin)
    ctx["accounts"] = rows
    return templates.TemplateResponse(
        request=request, name="accounts/list.html", context=ctx
    )
```

- [ ] **Step 4: Create the list templates**

Create `src/localmail/serve/admin/templates/accounts/list.html`:

```html
{% extends "base.html" %}
{% block title %}Accounts — localmail admin{% endblock %}
{% block content %}
<div class="admin-card">
  <div class="accounts-header">
    <h1>Accounts</h1>
    <a href="/admin/accounts/new" class="admin-button">+ New account</a>
  </div>
  <table class="accounts-table">
    <thead>
      <tr><th>Name</th><th>Email</th><th>Auth</th><th>Sync</th><th></th></tr>
    </thead>
    <tbody>
      {% for acct in accounts %}
        {% include "accounts/_row.html" %}
      {% else %}
      <tr><td colspan="5">No accounts configured.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

Create `src/localmail/serve/admin/templates/accounts/_row.html`:

```html
<tr id="account-row-{{ acct.id }}">
  <td><a href="/admin/accounts/{{ acct.id }}">{{ acct.name }}</a></td>
  <td>{{ acct.email_address }}</td>
  <td>{{ acct.auth_method }}</td>
  <td>{% if acct.auth_method == "archive" %}—
      {% elif acct.sync_enabled %}<span class="sync-on">enabled</span>
      {% else %}<span class="sync-off">paused</span>{% endif %}</td>
  <td class="account-row-actions">
    <a href="/admin/accounts/{{ acct.id }}">Edit</a>
    {% if acct.auth_method != "archive" %}
    <button type="button" class="link-button"
      hx-post="/admin/accounts/{{ acct.id }}/sync-toggle"
      hx-target="#account-row-{{ acct.id }}" hx-swap="outerHTML"
      hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/accounts/" ~ acct.id ~ "/sync-toggle") }}"}'>
      {% if acct.sync_enabled %}Disable{% else %}Enable{% endif %}</button>
    {% endif %}
    <button type="button" class="link-button danger"
      hx-post="/admin/accounts/{{ acct.id }}/delete"
      hx-target="#account-row-{{ acct.id }}" hx-swap="outerHTML"
      hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/accounts/" ~ acct.id ~ "/delete") }}"}'>
      Delete</button>
  </td>
</tr>
```

(`acct.sync_enabled` is present on `AccountSummary`; `auth_method` too. The `_row.html` partial is rendered both by the list loop and, later, by the sync-toggle/delete routes — it relies only on `acct` and `csrf_token_for_method` being in context.)

- [ ] **Step 5: Register the router**

In `src/localmail/serve/app.py`, add the import near the other admin router imports (around line 17):

```python
from localmail.serve.admin import accounts_panel_router as admin_accounts_panel_router
```

And register it alongside the other `/admin` HTML routers (after the `admin_daemon_panel_router` include, ~line 182):

```python
        app.include_router(admin_accounts_panel_router.router, prefix="/admin")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add src/localmail/serve/admin/accounts_panel_router.py \
        src/localmail/serve/admin/templates/accounts/list.html \
        src/localmail/serve/admin/templates/accounts/_row.html \
        src/localmail/serve/app.py tests/test_serve_admin_account_screens.py
git commit -m "feat(admin): account list screen (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Create form (GET /accounts/new) + create (POST /accounts)

**Files:**
- Modify: `src/localmail/serve/admin/accounts_panel_router.py`
- Create: `src/localmail/serve/admin/templates/accounts/form.html`
- Create: `src/localmail/serve/admin/templates/accounts/_form_fields.html`
- Test: `tests/test_serve_admin_account_screens.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_serve_admin_account_screens.py`. Helper to extract the form CSRF token, then create tests:

```python
def _csrf_header(client, path: str) -> dict:
    """Mint a method-bound POST token by scraping it from a rendered form/page.

    The new-account form embeds the create token in a hx-headers attribute on
    the <form>; we read it from there so the test exercises the real mint.
    """
    page = client.get("/admin/accounts/new").text
    m = re.search(r'data-create-csrf="([^"]+)"', page)
    assert m, "create CSRF token not found in new-account form"
    return {"X-CSRF-Token": m.group(1)}


def test_new_account_form_renders(admin_client):
    r = admin_client.get("/admin/accounts/new")
    assert r.status_code == 200
    assert 'name="auth_method"' in r.text
    assert "/admin/static/accounts-panel.js" in r.text


def test_create_account_happy_path(admin_client, db_conn):
    headers = _csrf_header(admin_client, "/admin/accounts")
    r = admin_client.post(
        "/admin/accounts",
        data={
            "name": "fastmail", "email_address": "me@fastmail.com",
            "auth_method": "password", "imap_host": "imap.fastmail.com",
            "imap_port": "993", "oauth_provider": "",
            "folder_allow": "INBOX", "folder_deny": "",
        },
        headers=headers,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect", "").startswith("/admin/accounts/")
    with db_conn.cursor() as cur:
        cur.execute("SELECT name FROM accounts WHERE name = 'fastmail'")
        assert cur.fetchone() is not None


def test_create_account_validation_error_inline(admin_client):
    headers = _csrf_header(admin_client, "/admin/accounts")
    r = admin_client.post(
        "/admin/accounts",
        data={
            "name": "x", "email_address": "x@x.com", "auth_method": "password",
            "imap_host": "h", "imap_port": "70000", "oauth_provider": "",
            "folder_allow": "", "folder_deny": "",
        },
        headers=headers,
    )
    assert r.status_code == 400
    assert "imap_port" in r.text


def test_create_account_missing_csrf_rejected(admin_client):
    r = admin_client.post(
        "/admin/accounts",
        data={"name": "y", "email_address": "y@y.com", "auth_method": "archive"},
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "new_account or create_account" -v`
Expected: FAIL — `/admin/accounts/new` 404s; POST route missing.

- [ ] **Step 3: Add the GET new + POST create routes**

Append to `accounts_panel_router.py` the imports, helpers, and routes. The POST route is `async` so it can `await request.form()` (Starlette parses form bodies asynchronously); the GET route stays sync (it reads no body).

```python
from fastapi import Header, HTTPException
from fastapi.responses import Response

from localmail.serve.admin import account_forms as forms
from localmail.serve.admin.csrf import check_csrf
from localmail.api.errors import NotFound


def _form_context(request: Request, admin: AdminUser, *, values: dict,
                  account_id: int | None, field_errors: dict | None = None,
                  oauth: str | None = None) -> dict:
    ctx = _base_context(request, admin)
    ctx.update({
        "values": values,
        "account_id": account_id,
        "field_errors": field_errors or {},
        "deny_flags": forms.DENY_FLAGS,
        "deny_flags_checked": values.get("deny_flags_checked", set()),
        "oauth": oauth,
    })
    return ctx


_BLANK_VALUES = {
    "name": "", "email_address": "", "auth_method": "password",
    "oauth_provider": "", "imap_host": "", "imap_port": "",
    "folder_allow": "", "folder_deny": "", "deny_flags_checked": set(),
    "sync_enabled": True,
}


def _rerender_form_error(request, admin, raw, deny, err, *, account_id):
    """Re-render the field fragment (400) with inline errors and the user's
    submitted values preserved."""
    values = {k: raw.get(k, "") for k in (
        "name", "email_address", "auth_method", "oauth_provider",
        "imap_host", "imap_port", "folder_allow", "folder_deny")}
    values["deny_flags_checked"] = set(deny)
    ctx = _form_context(request, admin, values=values, account_id=account_id,
                        field_errors=forms.field_errors_from(err))
    return templates.TemplateResponse(
        request=request, name="accounts/_form_fields.html", context=ctx,
        status_code=400,
    )


@router.get("/accounts/new", response_class=HTMLResponse)
def new_account_form(
    request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    ctx = _form_context(request, admin, values=dict(_BLANK_VALUES), account_id=None)
    return templates.TemplateResponse(
        request=request, name="accounts/form.html", context=ctx
    )


@router.post("/accounts")
async def create_account(
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
):
    check_csrf(request, admin, x_csrf_token, "/admin/accounts")
    raw = await request.form()
    deny = raw.getlist("deny_flags")
    try:
        kwargs = forms.form_to_create_kwargs(dict(raw), deny_flags_selected=deny)
    except forms.FormError as e:
        return _rerender_form_error(request, admin, raw, deny, e, account_id=None)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            acct = svc.create_account(conn, **kwargs)
        except svc.AccountFieldError as e:
            return _rerender_form_error(request, admin, raw, deny, e, account_id=None)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/admin/accounts/{acct.id}"
    return resp
```

Note: `dict(raw)` collapses the multi-valued `deny_flags` checkboxes to a single value, which is why `deny` is captured separately via `raw.getlist("deny_flags")` and passed explicitly to the form helpers.

- [ ] **Step 4: Create the form templates**

Create `src/localmail/serve/admin/templates/accounts/form.html`:

```html
{% extends "base.html" %}
{% block title %}{% if account_id %}Edit account{% else %}New account{% endif %} — localmail admin{% endblock %}
{% block content %}
<div class="admin-card">
  <h1>{% if account_id %}Edit account{% else %}New account{% endif %}</h1>
  {% if oauth == "success" %}<p class="admin-flash admin-flash-success">Gmail connected.</p>{% endif %}
  {% if oauth == "failed" %}<p class="admin-flash admin-flash-error">Gmail authorization failed.</p>{% endif %}

  {% if account_id %}
    {% set post_url = "/admin/accounts/" ~ account_id %}
  {% else %}
    {% set post_url = "/admin/accounts" %}
  {% endif %}

  <form id="account-form" class="account-form"
        hx-post="{{ post_url }}" hx-target="#account-form-fields" hx-swap="outerHTML"
        data-create-csrf="{{ csrf_token_for_method('POST', post_url) }}"
        hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", post_url) }}"}'>
    {% include "accounts/_form_fields.html" %}
  </form>
</div>
<script src="/admin/static/accounts-panel.js" defer></script>
{% endblock %}
```

Create `src/localmail/serve/admin/templates/accounts/_form_fields.html`:

```html
<div id="account-form-fields" data-auth="{{ values.auth_method }}">
  {% if field_errors._form %}<p class="admin-flash admin-flash-error">{{ field_errors._form }}</p>{% endif %}

  <label>Name
    {% if account_id %}
      <input name="name" value="{{ values.name }}" readonly>
    {% else %}
      <input name="name" value="{{ values.name }}" required>
    {% endif %}
    {% if field_errors.name %}<span class="field-error">{{ field_errors.name }}</span>{% endif %}
  </label>

  <label>Email
    <input name="email_address" value="{{ values.email_address }}" required>
    {% if field_errors.email_address %}<span class="field-error">{{ field_errors.email_address }}</span>{% endif %}
  </label>

  <label>Auth method
    <select name="auth_method" data-auth-select>
      {% for m in ["password", "oauth2", "archive"] %}
      <option value="{{ m }}" {% if values.auth_method == m %}selected{% endif %}>{{ m }}</option>
      {% endfor %}
    </select>
  </label>

  <div data-auth-group="password oauth2">
    <label>IMAP host
      <input name="imap_host" value="{{ values.imap_host }}">
      {% if field_errors.imap_host %}<span class="field-error">{{ field_errors.imap_host }}</span>{% endif %}
    </label>
    <label>IMAP port
      <input name="imap_port" value="{{ values.imap_port }}">
      {% if field_errors.imap_port %}<span class="field-error">{{ field_errors.imap_port }}</span>{% endif %}
    </label>
  </div>

  <div data-auth-group="oauth2">
    <label>OAuth provider
      <select name="oauth_provider">
        <option value="" {% if not values.oauth_provider %}selected{% endif %}>—</option>
        <option value="gmail" {% if values.oauth_provider == "gmail" %}selected{% endif %}>gmail</option>
      </select>
    </label>
    {% if account_id %}
    <button type="button" class="admin-button"
      hx-post="/admin/accounts/{{ account_id }}/oauth/start"
      hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/accounts/" ~ account_id ~ "/oauth/start") }}"}'>
      Connect Gmail</button>
    {% endif %}
  </div>

  <fieldset>
    <legend>Folder filters</legend>
    <label>Allow (one per line, blank = all)
      <textarea name="folder_allow" rows="3">{{ values.folder_allow }}</textarea>
    </label>
    <label>Deny (one per line)
      <textarea name="folder_deny" rows="2">{{ values.folder_deny }}</textarea>
    </label>
    <div class="deny-flags">
      {% for flag in deny_flags %}
      <label><input type="checkbox" name="deny_flags" value="{{ flag }}"
        {% if flag in deny_flags_checked %}checked{% endif %}> {{ flag }}</label>
      {% endfor %}
    </div>
  </fieldset>

  <div class="form-actions">
    <button type="submit" class="admin-button">Save</button>
  </div>
</div>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "new_account or create_account" -v`
Expected: PASS (4 tests). If `test_create_account_missing_csrf_rejected` fails because the body-wide `hx-headers` token from `base.html` is NOT sent by `TestClient` (it isn't — TestClient doesn't execute HTMX JS), the missing-header path correctly yields 400. Good.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/admin/accounts_panel_router.py \
        src/localmail/serve/admin/templates/accounts/form.html \
        src/localmail/serve/admin/templates/accounts/_form_fields.html \
        tests/test_serve_admin_account_screens.py
git commit -m "feat(admin): account create form + validation (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Edit (GET /accounts/{id}) + update (POST /accounts/{id})

**Files:**
- Modify: `src/localmail/serve/admin/accounts_panel_router.py`
- Test: `tests/test_serve_admin_account_screens.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_edit_form_prefills(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail", folder_allow=["INBOX"])
    r = admin_client.get(f"/admin/accounts/{aid}")
    assert r.status_code == 200
    assert "fastmail" in r.text
    assert "INBOX" in r.text


def test_edit_unknown_account_404(admin_client):
    r = admin_client.get("/admin/accounts/999999")
    assert r.status_code == 404


def test_edit_form_shows_oauth_success_flash(admin_client, db_conn):
    aid = _seed_account(db_conn, name="g", email_address="g@gmail.com",
                        auth_method="oauth2", imap_host="imap.gmail.com",
                        oauth_provider="gmail")
    r = admin_client.get(f"/admin/accounts/{aid}?oauth=success")
    assert r.status_code == 200
    assert "Gmail connected" in r.text


def test_update_account_changes_field(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    path = f"/admin/accounts/{aid}"
    page = admin_client.get(path).text
    m = re.search(r'data-create-csrf="([^"]+)"', page)
    assert m
    r = admin_client.post(
        path,
        data={
            "name": "fastmail", "email_address": "new@fastmail.com",
            "auth_method": "password", "imap_host": "imap.fastmail.com",
            "imap_port": "993", "oauth_provider": "",
            "folder_allow": "", "folder_deny": "",
        },
        headers={"X-CSRF-Token": m.group(1)},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == path
    with db_conn.cursor() as cur:
        cur.execute("SELECT email_address FROM accounts WHERE id = %s", (aid,))
        assert cur.fetchone()[0] == "new@fastmail.com"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "edit_form or update_account" -v`
Expected: FAIL — edit GET/POST routes missing (404/405).

- [ ] **Step 3: Add the edit + update routes**

```python
@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def edit_account_form(
    account_id: int, request: Request,
    oauth: str | None = None,
    admin: AdminUser = require_admin_session(),
) -> HTMLResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            acct = svc.get_account(conn, account_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
    values = forms.account_to_form_values(acct)
    ctx = _form_context(request, admin, values=values, account_id=account_id,
                        oauth=oauth)
    return templates.TemplateResponse(
        request=request, name="accounts/form.html", context=ctx
    )


@router.post("/accounts/{account_id}")
async def update_account(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
):
    check_csrf(request, admin, x_csrf_token, f"/admin/accounts/{account_id}")
    raw = await request.form()
    deny = raw.getlist("deny_flags")
    try:
        fields = forms.form_to_patch_fields(dict(raw), deny_flags_selected=deny)
    except forms.FormError as e:
        return _rerender_form_error(request, admin, raw, deny, e,
                                    account_id=account_id)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.update_account(conn, account_id, **fields)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountFieldError as e:
            return _rerender_form_error(request, admin, raw, deny, e,
                                        account_id=account_id)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/admin/accounts/{account_id}"
    return resp
```

**Route-ordering note:** FastAPI matches in declaration order. `GET /accounts/new` MUST be declared before `GET /accounts/{account_id}` or "new" gets captured as an id. Verify `new_account_form` appears above `edit_account_form` in the file; if not, move it up. (Path params are typed `int`, so `/accounts/new` wouldn't coerce — but keep new first for clarity.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "edit_form or update_account" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/accounts_panel_router.py tests/test_serve_admin_account_screens.py
git commit -m "feat(admin): account edit + update (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Store password (POST /accounts/{id}/password)

**Files:**
- Modify: `src/localmail/serve/admin/accounts_panel_router.py`
- Create: `src/localmail/serve/admin/templates/accounts/_secret_status.html`
- Test: `tests/test_serve_admin_account_screens.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_store_password_for_password_account(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    path = f"/admin/accounts/{aid}/password"
    page = admin_client.get(f"/admin/accounts/{aid}").text
    # the password sub-form embeds its own method-bound token
    m = re.search(r'data-password-csrf="([^"]+)"', page)
    assert m
    r = admin_client.post(
        path, data={"password": "s3cret"}, headers={"X-CSRF-Token": m.group(1)}
    )
    assert r.status_code == 200
    assert "stored" in r.text.lower()


def test_store_password_rejected_for_oauth_account(admin_client, db_conn):
    aid = _seed_account(db_conn, name="g", email_address="g@gmail.com",
                        auth_method="oauth2", imap_host="imap.gmail.com",
                        oauth_provider="gmail")
    # mint a token via csrf helper directly
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=_admin_id_of(admin_client),
        action=csrf_action("POST", f"/admin/accounts/{aid}/password"),
        key=_SIGNING_KEY.encode("ascii"),
    )
    r = admin_client.post(
        f"/admin/accounts/{aid}/password",
        data={"password": "x"}, headers={"X-CSRF-Token": tok},
    )
    assert r.status_code == 400
```

Add a tiny helper near the top of the test module to recover the admin id for direct token minting (the fixture user is "horst"):

```python
def _admin_id_of(client) -> int:
    # The session was created for username "horst"; look it up via the page.
    # Simpler: store id on the client in the admin_client fixture.
    return client.app_state_admin_id  # set in admin_client fixture below
```

Update the `admin_client` fixture to stash the id:

```python
@pytest.fixture
def admin_client(app, admin_user_id):
    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": m.group(1)},
    )
    assert r.status_code == 303, r.text
    client.app_state_admin_id = admin_user_id
    return client
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "store_password" -v`
Expected: FAIL — password route + `data-password-csrf` token + `_secret_status.html` missing.

- [ ] **Step 3: Add the password sub-form to `_form_fields.html`**

Inside the `data-auth-group="password"` region of `_form_fields.html`, add (only meaningful when editing — needs an account id):

```html
  {% if account_id %}
  <div data-auth-group="password" class="password-subform">
    <label>IMAP password
      <input type="password" name="password"
        hx-post="/admin/accounts/{{ account_id }}/password"
        hx-trigger="none"
        data-password-csrf="{{ csrf_token_for_method('POST', '/admin/accounts/' ~ account_id ~ '/password') }}">
    </label>
    <button type="button" class="admin-button" data-store-password
      hx-post="/admin/accounts/{{ account_id }}/password"
      hx-include="[name='password']"
      hx-target="#secret-status" hx-swap="innerHTML"
      hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/accounts/" ~ account_id ~ "/password") }}"}'>
      Store password</button>
    <span id="secret-status"></span>
  </div>
  {% endif %}
```

Note the existing `data-auth-group="password oauth2"` host/port block stays; this is an additional password-only block. The toggle JS (Task 11) shows/hides any element whose `data-auth-group` contains the current method.

Create `src/localmail/serve/admin/templates/accounts/_secret_status.html`:

```html
<span class="secret-ok">✅ Password stored.</span>
```

- [ ] **Step 4: Add the password route**

```python
@router.post("/accounts/{account_id}/password", response_class=HTMLResponse)
async def store_password(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/password")
    raw = await request.form()
    password = str(raw.get("password", ""))
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            account = svc.get_account(conn, account_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
    try:
        svc.store_password(account, password)
    except svc.AccountFieldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with pool.connection() as conn:
        svc.touch_account_updated_at(conn, account_id)
    return templates.TemplateResponse(
        request=request, name="accounts/_secret_status.html",
        context=_base_context(request, admin),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "store_password" -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/admin/accounts_panel_router.py \
        src/localmail/serve/admin/templates/accounts/_form_fields.html \
        src/localmail/serve/admin/templates/accounts/_secret_status.html \
        tests/test_serve_admin_account_screens.py
git commit -m "feat(admin): store IMAP password from edit screen (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Test-connection (POST /accounts/{id}/test-connection)

**Files:**
- Modify: `src/localmail/serve/admin/accounts_panel_router.py`
- Create: `src/localmail/serve/admin/templates/accounts/_test_result.html`
- Test: `tests/test_serve_admin_account_screens.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_test_connection_lists_folders(admin_client, db_conn, monkeypatch):
    aid = _seed_account(db_conn, name="fastmail")
    monkeypatch.setattr(
        svc, "probe_connection",
        lambda conn, account_id, gmail_client_secrets=None: [
            svc.FolderInfo(name="INBOX", flags=(r"\HasNoChildren",)),
            svc.FolderInfo(name="Spam", flags=(r"\Junk",)),
        ],
    )
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=admin_client.app_state_admin_id,
        action=csrf_action("POST", f"/admin/accounts/{aid}/test-connection"),
        key=_SIGNING_KEY.encode("ascii"),
    )
    r = admin_client.post(
        f"/admin/accounts/{aid}/test-connection", headers={"X-CSRF-Token": tok}
    )
    assert r.status_code == 200
    assert "INBOX" in r.text and "Spam" in r.text


def test_test_connection_error_renders_inline(admin_client, db_conn, monkeypatch):
    aid = _seed_account(db_conn, name="fastmail")
    def boom(conn, account_id, gmail_client_secrets=None):
        raise svc.AccountFieldError("no password stored")
    monkeypatch.setattr(svc, "probe_connection", boom)
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=admin_client.app_state_admin_id,
        action=csrf_action("POST", f"/admin/accounts/{aid}/test-connection"),
        key=_SIGNING_KEY.encode("ascii"),
    )
    r = admin_client.post(
        f"/admin/accounts/{aid}/test-connection", headers={"X-CSRF-Token": tok}
    )
    assert r.status_code == 200
    assert "no password stored" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "test_connection" -v`
Expected: FAIL — route + template missing.

- [ ] **Step 3: Add the button, template, and route**

In `_form_fields.html`, inside `data-auth-group="password oauth2"`, add a test button + result target (only when editing):

```html
  {% if account_id %}
  <button type="button" class="admin-button"
    hx-post="/admin/accounts/{{ account_id }}/test-connection"
    hx-target="#test-result" hx-swap="innerHTML"
    hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/accounts/" ~ account_id ~ "/test-connection") }}"}'>
    Test connection</button>
  <div id="test-result"></div>
  {% endif %}
```

Create `src/localmail/serve/admin/templates/accounts/_test_result.html`:

```html
{% if error %}
  <p class="admin-flash admin-flash-error">{{ error }}</p>
{% else %}
  <ul class="folder-list">
    {% for f in folders %}
    <li>{{ f.name }} <span class="folder-flags">{{ f.flags | join(" ") }}</span></li>
    {% endfor %}
  </ul>
{% endif %}
```

Add the route (renders the fragment for both success and the AccountFieldError case — a 200 fragment, since HTMX swaps it either way):

```python
@router.post("/accounts/{account_id}/test-connection", response_class=HTMLResponse)
def test_connection(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/test-connection")
    secrets_path = getattr(request.app.state, "gmail_client_secrets_file", None)
    ctx = _base_context(request, admin)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            folders = svc.probe_connection(
                conn, account_id, gmail_client_secrets=secrets_path
            )
            ctx["folders"] = folders
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountFieldError as e:
            ctx["error"] = str(e)
    return templates.TemplateResponse(
        request=request, name="accounts/_test_result.html", context=ctx
    )
```

(The route is sync — `probe_connection` does its own blocking IMAP IO; FastAPI runs sync routes in a threadpool, which is correct for blocking IO.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "test_connection" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/accounts_panel_router.py \
        src/localmail/serve/admin/templates/accounts/_form_fields.html \
        src/localmail/serve/admin/templates/accounts/_test_result.html \
        tests/test_serve_admin_account_screens.py
git commit -m "feat(admin): test-connection fragment on edit screen (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Sync-toggle (POST /accounts/{id}/sync-toggle)

**Files:**
- Modify: `src/localmail/serve/admin/accounts_panel_router.py`
- Test: `tests/test_serve_admin_account_screens.py`

- [ ] **Step 1: Write the failing tests**

```python
def _post_with_token(admin_client, path):
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=admin_client.app_state_admin_id,
        action=csrf_action("POST", path),
        key=_SIGNING_KEY.encode("ascii"),
    )
    return admin_client.post(path, headers={"X-CSRF-Token": tok})


def test_sync_toggle_disables_then_enables(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")  # sync_enabled defaults TRUE
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/sync-toggle")
    assert r.status_code == 200
    assert f'id="account-row-{aid}"' in r.text
    assert "Enable" in r.text  # now paused → button offers Enable
    with db_conn.cursor() as cur:
        cur.execute("SELECT sync_enabled FROM accounts WHERE id = %s", (aid,))
        assert cur.fetchone()[0] is False
    r2 = _post_with_token(admin_client, f"/admin/accounts/{aid}/sync-toggle")
    assert "Disable" in r2.text


def test_sync_toggle_unknown_404(admin_client):
    r = _post_with_token(admin_client, "/admin/accounts/999999/sync-toggle")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "sync_toggle" -v`
Expected: FAIL — route missing.

- [ ] **Step 3: Add the route (renders `_row.html` with a fresh `AccountSummary`)**

```python
@router.post("/accounts/{account_id}/sync-toggle", response_class=HTMLResponse)
def sync_toggle(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/sync-toggle")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            acct = svc.get_account(conn, account_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        if acct.auth_method == "archive":
            raise HTTPException(status_code=400, detail="archive accounts do not sync")
        svc.update_account(conn, account_id, sync_enabled=not acct.sync_enabled)
        summaries = {s.id: s for s in svc.list_accounts(conn)}
    ctx = _base_context(request, admin)
    ctx["acct"] = summaries[account_id]
    return templates.TemplateResponse(
        request=request, name="accounts/_row.html", context=ctx
    )
```

(`_row.html` consumes an `AccountSummary` via `acct`; `list_accounts` returns summaries, so re-reading after the update gives the swapped row its new state.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "sync_toggle" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/accounts_panel_router.py tests/test_serve_admin_account_screens.py
git commit -m "feat(admin): enable/disable sync row toggle (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Delete (POST /accounts/{id}/delete) — cascade-or-refuse

**Files:**
- Modify: `src/localmail/serve/admin/accounts_panel_router.py`
- Create: `src/localmail/serve/admin/templates/accounts/_delete_confirm.html`
- Test: `tests/test_serve_admin_account_screens.py`

- [ ] **Step 1: Write the failing tests**

```python
def _seed_message_for(db_conn, account_id):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'INBOX') "
            "RETURNING id", (account_id,))
        mid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, mailbox_id, uid, raw_bytes, "
            "size_bytes, headers, attachments, raw_sha256) "
            "VALUES (%s, %s, 1, %s, 3, '{}'::jsonb, '[]'::jsonb, %s)",
            (account_id, mid, b"abc", "a" * 64))
    db_conn.commit()


def test_delete_empty_account_removes_row(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/delete")
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/admin/accounts"
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (aid,))
        assert cur.fetchone() is None


def test_delete_in_use_offers_force_confirm(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    _seed_message_for(db_conn, aid)
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/delete")
    assert r.status_code == 409
    assert "force" in r.text.lower()
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (aid,))
        assert cur.fetchone() is not None


def test_delete_force_removes_in_use_account(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    _seed_message_for(db_conn, aid)
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=admin_client.app_state_admin_id,
        action=csrf_action("POST", f"/admin/accounts/{aid}/delete"),
        key=_SIGNING_KEY.encode("ascii"),
    )
    r = admin_client.post(
        f"/admin/accounts/{aid}/delete?force=1", headers={"X-CSRF-Token": tok}
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/admin/accounts"
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (aid,))
        assert cur.fetchone() is None
```

Confirm the exact `messages`/`mailboxes` insert column shape against `migrations/0001_init.sql` before running (the NOT NULL set is `account_id, mailbox_id, uid, raw_bytes, size_bytes, headers, attachments`; `raw_sha256` is needed for the no-message-id dedup index). Adjust the helper if a column differs.

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "delete" -v`
Expected: FAIL — delete route + confirm template missing.

- [ ] **Step 3: Add the confirm template + route**

Create `src/localmail/serve/admin/templates/accounts/_delete_confirm.html`:

```html
<div class="delete-confirm">
  <p class="admin-flash admin-flash-error">
    This account still has messages. Force-delete will remove the account row
    (messages and attachment references are cascaded by the DB).</p>
  <button type="button" class="admin-button danger"
    hx-post="/admin/accounts/{{ account_id }}/delete?force=1"
    hx-target="#account-row-{{ account_id }}" hx-swap="outerHTML"
    hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/admin/accounts/" ~ account_id ~ "/delete") }}"}'>
    Force delete</button>
</div>
```

Add the route:

```python
from fastapi import Query

@router.post("/accounts/{account_id}/delete", response_class=HTMLResponse)
def delete_account(
    account_id: int, request: Request,
    force: bool = Query(False),
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/delete")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_account(conn, account_id, force=force)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountInUse:
            ctx = _base_context(request, admin)
            ctx["account_id"] = account_id
            return templates.TemplateResponse(
                request=request, name="accounts/_delete_confirm.html",
                context=ctx, status_code=409,
            )
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/admin/accounts"
    return resp  # type: ignore[return-value]
```

(The 200+`HX-Redirect` response is a bare `Response`, not HTML; the `response_class=HTMLResponse` only affects the default for template returns. Returning `Response` directly is fine.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "delete" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/accounts_panel_router.py \
        src/localmail/serve/admin/templates/accounts/_delete_confirm.html \
        tests/test_serve_admin_account_screens.py
git commit -m "feat(admin): delete account with cascade-or-refuse confirm (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Gmail OAuth start (POST /accounts/{id}/oauth/start)

**Files:**
- Modify: `src/localmail/serve/admin/accounts_panel_router.py`
- Test: `tests/test_serve_admin_account_screens.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_oauth_start_redirects_to_google(admin_client, db_conn, monkeypatch):
    aid = _seed_account(db_conn, name="g", email_address="g@gmail.com",
                        auth_method="oauth2", imap_host="imap.gmail.com",
                        oauth_provider="gmail")
    from localmail.api.admin import oauth as oauth_svc
    monkeypatch.setattr(
        oauth_svc, "start_oauth",
        lambda conn, account_id, **kw: "https://accounts.google.com/o/oauth2/auth?x=1",
    )
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/oauth/start")
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://accounts.google.com/")


def test_oauth_start_not_configured_is_503(admin_client, db_conn, monkeypatch):
    aid = _seed_account(db_conn, name="g", email_address="g@gmail.com",
                        auth_method="oauth2", imap_host="imap.gmail.com",
                        oauth_provider="gmail")
    from localmail.api.admin import oauth as oauth_svc
    def boom(conn, account_id, **kw):
        raise oauth_svc.OAuthNotConfigured("Gmail OAuth is not configured")
    monkeypatch.setattr(oauth_svc, "start_oauth", boom)
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/oauth/start")
    assert r.status_code == 503
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "oauth_start" -v`
Expected: FAIL — route missing.

- [ ] **Step 3: Add the route**

Add imports and the route. It reuses the same state-signing key + callback URL helpers the JSON oauth router uses (read straight from `request.app.state.serve_config`):

```python
from fastapi.responses import RedirectResponse
from localmail.api.admin import oauth as oauth_svc

_HTTP_SEE_OTHER = 303


@router.post("/accounts/{account_id}/oauth/start")
def oauth_start(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
):
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/oauth/start")
    cfg = request.app.state.serve_config
    secrets_path = getattr(request.app.state, "gmail_client_secrets_file", None)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            url = oauth_svc.start_oauth(
                conn, account_id,
                admin_user_id=admin.id,
                signing_key=cfg.state_signing_key.encode("ascii"),
                redirect_uri=cfg.oauth_callback_url,
                client_secrets_file=secrets_path,
            )
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except oauth_svc.AccountFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except oauth_svc.OAuthNotConfigured as e:
            raise HTTPException(status_code=503, detail=str(e))
    return RedirectResponse(url, status_code=_HTTP_SEE_OTHER)
```

Confirm `start_oauth`'s signature and exception names by reading `src/localmail/api/admin/oauth.py` (it mirrors the JSON `oauth_router.oauth_start` call). The callback `GET /admin/oauth/callback` is already registered and already redirects to `/admin/accounts/{id}?oauth=success` — no change.

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "oauth_start" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/accounts_panel_router.py tests/test_serve_admin_account_screens.py
git commit -m "feat(admin): Gmail OAuth start from edit screen (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Auth-method field toggle JS + CSP test + styling

**Files:**
- Create: `src/localmail/serve/admin/static/accounts-panel.js`
- Modify: `src/localmail/serve/admin/static/admin.css` (append styles)
- Test: `tests/test_serve_admin_account_screens.py`

- [ ] **Step 1: Write the failing CSP/static test**

```python
def test_form_references_static_js_not_inline(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    r = admin_client.get(f"/admin/accounts/{aid}")
    assert "/admin/static/accounts-panel.js" in r.text
    # No inline event handlers / inline <script> bodies (CSP script-src 'self')
    assert "onclick=" not in r.text
    assert "hx-on:" not in r.text


def test_accounts_panel_js_is_served(admin_client):
    r = admin_client.get("/admin/static/accounts-panel.js")
    assert r.status_code == 200
    assert "data-auth-select" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "static_js or panel_js" -v`
Expected: FAIL — `accounts-panel.js` 404s.

- [ ] **Step 3: Create the served-static JS**

Create `src/localmail/serve/admin/static/accounts-panel.js`:

```javascript
// Account form: show/hide field groups by the selected auth method.
// CSP for /admin is `script-src 'self'` (no inline handlers), so all behaviour
// is bound here from a served file (mirrors daemon-panel.js).
(function () {
  "use strict";

  function applyAuthVisibility(root) {
    var select = root.querySelector("[data-auth-select]");
    if (!select) return;
    var method = select.value;
    var groups = root.querySelectorAll("[data-auth-group]");
    groups.forEach(function (el) {
      var applies = el.getAttribute("data-auth-group").split(/\s+/);
      el.hidden = applies.indexOf(method) === -1;
    });
  }

  function wire(root) {
    var select = root.querySelector("[data-auth-select]");
    if (!select) return;
    select.addEventListener("change", function () { applyAuthVisibility(root); });
    applyAuthVisibility(root);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var fields = document.getElementById("account-form-fields");
    if (fields) wire(fields);
  });

  // Re-wire after an HTMX swap replaces #account-form-fields (validation error).
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var fields = evt.target.querySelector
      ? (evt.target.id === "account-form-fields"
          ? evt.target
          : evt.target.querySelector("#account-form-fields"))
      : null;
    if (fields) wire(fields);
  });
})();
```

- [ ] **Step 4: Append minimal styles to `admin.css`**

Append to `src/localmail/serve/admin/static/admin.css`:

```css
.accounts-header { display: flex; justify-content: space-between; align-items: center; }
.accounts-table { width: 100%; border-collapse: collapse; }
.accounts-table th, .accounts-table td { padding: .4rem; text-align: left; border-bottom: 1px solid #e2e2e2; }
.account-row-actions { white-space: nowrap; }
.link-button { background: none; border: none; color: #2563eb; cursor: pointer; padding: 0 .3rem; }
.link-button.danger { color: #b91c1c; }
.field-error { color: #b91c1c; display: block; font-size: .85rem; }
.sync-on { color: #15803d; }
.sync-off { color: #92400e; }
.folder-list { margin: .4rem 0; }
.folder-flags { color: #6b7280; font-size: .85rem; }
[data-auth-group][hidden] { display: none; }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py -k "static_js or panel_js" -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Confirm the CSP suite still passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_csp.py -q`
Expected: PASS (the new page serves under the existing `script-src 'self'` CSP; no inline script added).

- [ ] **Step 7: Commit**

```bash
git add src/localmail/serve/admin/static/accounts-panel.js \
        src/localmail/serve/admin/static/admin.css \
        tests/test_serve_admin_account_screens.py
git commit -m "feat(admin): auth-method field toggle JS + styles (2A.3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Non-admin gating test + full-suite green + docs

**Files:**
- Test: `tests/test_serve_admin_account_screens.py`
- Modify: `README.md` (admin UI section), `CLAUDE.md` (GUI server section)

- [ ] **Step 1: Add the non-admin gating test**

```python
@pytest.fixture
def nonadmin_client(app, db_conn):
    pwh = hash_password("pw")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('bob', %s, FALSE)", (pwh,))
    db_conn.commit()
    return TestClient(app, follow_redirects=False)


def test_nonadmin_cannot_reach_accounts(nonadmin_client):
    # No admin session cookie at all → redirect to login.
    r = nonadmin_client.get("/admin/accounts")
    assert r.status_code in (302, 303)
    assert "/admin/login" in r.headers["location"]
```

- [ ] **Step 2: Run the whole new test module + the admin suites**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_admin_account_screens.py tests/test_account_forms.py tests/test_admin_accounts.py tests/test_serve_admin_accounts.py tests/test_serve_admin_csp.py -q`
Expected: PASS.

- [ ] **Step 3: Full suite + mypy**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/`
Expected: PASS (1241 prior + the new tests).

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean.

- [ ] **Step 4: Verify `accounts_panel_router.py` is under 500 lines**

Run: `wc -l src/localmail/serve/admin/accounts_panel_router.py`
Expected: < 500. If it exceeds, split the OAuth + test-connection handlers into `accounts_panel_actions.py` and re-export, as the spec's Conventions note allows.

- [ ] **Step 5: Update README.md**

In the admin/GUI section of `README.md`, add `/admin/accounts` to the list of admin screens with a one-line description: list/create/edit/delete IMAP accounts, store passwords, run test-connection, connect Gmail, enable/disable sync. Note the OAuth callback lands on the edit page.

- [ ] **Step 6: Update CLAUDE.md**

In the "GUI server" section of `CLAUDE.md`, add a 2A.3 bullet: account CRUD admin screens at `/admin/accounts` (server-rendered HTMX partials in `serve/admin/accounts_panel_router.py` + `account_forms.py`); method-bound CSRF closes #125; `probe_connection` now supports oauth2 (threaded Gmail secrets); no new migration. Mark #125 resolved.

- [ ] **Step 7: Commit**

```bash
git add tests/test_serve_admin_account_screens.py README.md CLAUDE.md
git commit -m "test(admin): non-admin gating + docs for account screens (2A.3)

Closes #125.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review notes (for the implementer)

- **Spec coverage:** Task 1 = oauth2 probe; Task 2 = pure form helpers (folder/flag parsing, error mapping); Tasks 3–10 = the route table row-for-row (list, new, create, edit, update, password, test-connection, sync-toggle, delete, oauth-start); Task 11 = CSP-safe JS + method-toggle; Task 12 = auth gating + docs + #125 closure. The OAuth *callback* needs no task (already shipped).
- **CSRF:** every mutating route calls `check_csrf(request, admin, token, "<path>")`; templates mint with `csrf_token_for_method("POST", "<path>")`. `check_csrf` binds `POST:<path>`, so the mint and verify agree. The `_csrf_header`/`_post_with_token` test helpers exercise the real mint.
- **Async vs sync routes:** routes that read a form body (`create_account`, `update_account`, `store_password`) are `async` and `await request.form()`. Routes doing blocking IMAP IO (`test_connection`) stay sync (threadpool). GET renders + token-only POSTs (`sync-toggle`, `delete`, `oauth-start`) are sync.
- **Known pre-write check:** confirm the `messages`/`mailboxes` insert columns in Task 9 against `migrations/0001_init.sql` and the `start_oauth` signature in Task 10 against `api/admin/oauth.py` before running those tasks — both are flagged inline.
```
