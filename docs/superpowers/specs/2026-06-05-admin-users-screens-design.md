# Admin user-management screens (Sub-plan 2A.4) — design

> **Status:** design approved 2026-06-05. Closes the `/admin/users` 404 (the
> nav link already exists in `serve/admin/templates/base.html`). Mirrors the
> 2A.3 account-CRUD admin screens
> ([2026-05-17-localmail-gui-design.md](2026-05-17-localmail-gui-design.md) +
> the per-user ACL design
> [2026-05-18-per-user-account-acl-design.md](2026-05-18-per-user-account-acl-design.md)).

## Problem

The admin nav exposes a `/admin/users` link that currently 404s. There is no
web UI for managing API users — operators must use the CLI (`add-api-user`,
`list-api-users`, `remove-api-user`, `grant-account`, `revoke-account`,
`grant-admin`, `revoke-admin`, `revoke-admin-sessions`). The per-user account
ACL (`user_accounts`, migration 0016) is the security boundary for every
account-scoped read, so granting/revoking it is a core operator task that
deserves a first-class screen.

This sub-plan ships server-rendered HTMX screens at `/admin/users` plus a
machine-facing JSON API at `/v1/admin/users`, at full parity with the CLI.

## Goals

- List / create / delete API users from the admin UI.
- Grant / revoke per-account ACL from a user's edit screen (the headline value).
- Toggle the `is_admin` flag and revoke a user's outstanding admin sessions.
- Reset a user's password (admin reset — no old password required) and
  enable/disable a user (`disabled_at`) without deleting them.
- Guard against lock-out: refuse any action that would orphan the last active
  admin, and refuse self-delete / self-demote of the logged-in admin.
- Both an HTML panel and a JSON `/v1` router, sharing one service layer —
  exactly the 2A.3 accounts shape.

## Non-goals (YAGNI for v1)

- Individual token listing / revocation per user. Delete-user (cascades tokens)
  + `revoke-admin-sessions` cover the operational security need.
- User self-service password change via this UI — that is
  `auth.change_password` (requires the old password), a separate
  authenticated-user flow, not an admin screen.
- Bulk grant operations.
- Pagination of the user list. Rosters are small; a flat `ORDER BY username` is
  fine. Filed as a follow-up only if a deployment ever has hundreds of users.

## No new migration

`is_admin`, `disabled_at` (0014/0022), `sessions_invalidated_at` (0022), and
`user_accounts` (0016) all already exist. Latest applied migration stays
`0025_transient_extractions.sql`. The last-admin guard is a `SELECT count(*)`,
not a schema change.

> **Nullability note:** `api_users.is_admin BOOLEAN DEFAULT FALSE` (added in
> 0022) is *nullable*. Every admin predicate uses `is_admin IS TRUE`, never
> `is_admin = TRUE` or a bare truthiness check, so legacy NULL rows are treated
> as non-admin.

## Architecture (approach B — service module mirrors `api/admin/accounts.py`)

Three layers, mirroring 2A.3:

```
api/admin/users.py            # service layer (composes existing primitives + new ones)
serve/admin/users_router.py   # JSON  /v1/admin/users  (thin)
serve/admin/users_panel_router.py  # HTML /admin/users  (thin)
serve/admin/user_forms.py     # pure form parsing + error mapping
serve/admin/templates/users/  # Jinja templates + fragments
serve/admin/static/users-panel.js  # minimal, served file (CSP script-src 'self')
```

The safety-critical logic is split deliberately:

- **Count-based last-admin rule** lives in the service as a pure predicate
  (`would_orphan_last_admin`) plus an IO wrapper that reads the active-admin
  count. It is identity-agnostic and fully unit-testable.
- **Identity-based self-action rule** ("you can't delete/demote *yourself*")
  lives in the routers — the only layer that knows who the logged-in admin is.

Disabling unsafe controls in the templates is UX only; the service + router
guards are the real enforcement, so a hand-crafted POST still gets a 409 /
inline error.

### Service layer — `src/localmail/api/admin/users.py`

All functions take `conn`; the caller commits (same contract as the rest of
`api/`). The module **composes** existing primitives — `auth.create_user`,
`auth.hash_password`, `admin.auth.grant_admin` / `revoke_admin` /
`revoke_admin_sessions`, `acl.grant_account` / `revoke_account` /
`grants_for_user` — and adds what is missing.

**Dataclasses (frozen):**

- `UserSummary(id: int, username: str, is_admin: bool, disabled: bool,
  created_at: datetime)` — list screen.
- `AccountGrant(account_id: int, account_name: str, granted: bool)`.
- `UserDetail(id, username, is_admin, disabled, created_at,
  account_grants: list[AccountGrant])` — covers **every** account with a
  `granted` flag so the edit screen renders the full grant checklist from one
  service call.

**Errors (mirror `AccountFieldError`):**

- `UserFieldError(ValueError)` — validation (e.g. duplicate username, blank
  field). Maps to 400 at both transports.
- `LastAdminError(ValueError)` — the action would orphan the last active admin.
  Maps to 409.
- `SelfActionError(ValueError)` — raised by the **routers** for self-delete /
  self-demote. Maps to 409. (Defined alongside the service errors for a single
  import site, but only the routers raise it.)
- Reuse the existing `UserNotFound` from `api/admin/auth.py`.

**Functions:**

| Function | Behaviour |
|----------|-----------|
| `list_users(conn) -> list[UserSummary]` | `SELECT … ORDER BY username`. `is_admin IS TRUE`. |
| `get_user(conn, user_id) -> UserDetail` | `api_users` row + LEFT JOIN over `accounts`/`user_accounts` to build `account_grants` for every account. Raises `UserNotFound`. |
| `create_user(conn, username, password, *, is_admin=False) -> int` | Wraps `auth.create_user`, then `grant_admin` when `is_admin`. Maps `UniqueViolation` → `UserFieldError("username already taken")`. |
| `set_password(conn, user_id, new_password)` | Admin reset — `UPDATE password_hash`, **no old-password check**. Raises `UserNotFound` on missing row; `UserFieldError` on empty password. |
| `set_disabled(conn, user_id, disabled: bool)` | `disabled_at = now()` / `NULL`. When disabling, runs the last-admin guard. |
| `set_admin(conn, user_id, is_admin: bool)` | Wraps `grant_admin` / `revoke_admin` **by id**. When revoking, runs the last-admin guard. |
| `delete_user(conn, user_id)` | `DELETE FROM api_users` (tokens + grants cascade). Runs the last-admin guard. |
| `set_grant(conn, user_id, account_id, granted: bool)` | Wraps `acl.grant_account` / `revoke_account`. |
| `revoke_sessions(conn, user_id)` | Wraps `revoke_admin_sessions` by id (button on edit screen). |

**Pure guard predicate (unit-tested in isolation):**

```python
def would_orphan_last_admin(
    *,
    target_is_active_admin: bool,   # target currently counts toward the active-admin total
    active_admin_count: int,        # SELECT count(*) WHERE is_admin IS TRUE AND disabled_at IS NULL
) -> bool:
    """True iff removing the target's active-admin status drops the count to 0."""
    return target_is_active_admin and active_admin_count <= 1
```

The IO wrappers (`set_admin(False)`, `set_disabled(True)`, `delete_user`) read
the count and the target's current active-admin status, call the predicate, and
raise `LastAdminError` when it returns `True`.

### JSON router — `src/localmail/serve/admin/users_router.py` → `/v1/admin/users`

Thin wrapper, mirroring `accounts_router.py`: `require_admin_session` on every
route; method-bound CSRF (`X-CSRF-Token`, bound to `(user_id, "<METHOD>:<url>")`)
on every mutating route; `parse_int_id` on the wire `user_id`; Pydantic request
models; IDs emitted as strings.

| Method + path | Body | Success | Errors |
|---------------|------|---------|--------|
| `GET /v1/admin/users` | — | 200 `[{id, username, is_admin, disabled, created_at}]` | — |
| `POST /v1/admin/users` | `{username, password, is_admin?}` | 201 summary | `UserFieldError` → 400 |
| `GET /v1/admin/users/{id}` | — | 200 detail incl. `account_grants` | `UserNotFound` → 404 |
| `PATCH /v1/admin/users/{id}` | `{is_admin?, disabled?}` | 200 detail | `LastAdminError`/`SelfActionError` → 409; `UserNotFound` → 404 |
| `POST /v1/admin/users/{id}/password` | `{password}` | 200 | `UserFieldError` → 400; `UserNotFound` → 404 |
| `POST /v1/admin/users/{id}/grants` | `{account_id, granted}` | 200 detail | `UserNotFound` → 404 |
| `POST /v1/admin/users/{id}/revoke-sessions` | — | 200 | `UserNotFound` → 404 |
| `DELETE /v1/admin/users/{id}` | — | 200/204 | `SelfActionError`/`LastAdminError` → 409; `UserNotFound` → 404 |

`409` for guard violations mirrors the accounts cascade-refuse 409
(`AccountInUse`): a structured, actionable conflict, not a 400 or an opaque 500.
`400` is reserved for validation (`UserFieldError`), uniform with the accounts
`AccountFieldError → 400` mapping.

Self-action: the `PATCH` (when it would demote the caller) and `DELETE` handlers
compare `parse_int_id(user_id)` against `admin.id` and raise `SelfActionError`.

### HTML panel — `src/localmail/serve/admin/users_panel_router.py` → `/admin/users`

Mirrors `accounts_panel_router.py`: blocking DB calls offloaded via
`run_in_threadpool`; method-bound CSRF via `csrf_token_for_method`; inline error
fragments; `_base_context` / `_form_context` helpers.

| Method + path | Renders |
|---------------|---------|
| `GET /admin/users` | `users/list.html` (table: username, admin badge, disabled badge, actions) |
| `GET /admin/users/new` | blank `users/form.html` |
| `POST /admin/users` | create; 400 → re-render form with inline errors; success → `HX-Redirect` to edit page |
| `GET /admin/users/{id}` | `users/form.html` — edit: admin toggle, enable/disable toggle, password-reset field, **per-account grant checklist**, revoke-sessions button, delete button |
| `POST /admin/users/{id}/admin-toggle` | `set_admin`; guard violation → 200 + inline error fragment (`_row.html`/`_admin_toggle.html`) |
| `POST /admin/users/{id}/disable-toggle` | `set_disabled`; same guard handling |
| `POST /admin/users/{id}/password` | `set_password` → `users/_secret_status.html` |
| `POST /admin/users/{id}/grants` | `set_grant` → `users/_grants.html` |
| `POST /admin/users/{id}/revoke-sessions` | `revoke_sessions` → `users/_secret_status.html`-style confirmation |
| `POST /admin/users/{id}/delete` | confirm-then-delete; self/last-admin → `users/_delete_blocked.html` (409); success → `HX-Redirect` to `/admin/users` |

Server-side, the edit screen renders self/last-admin-unsafe controls as
`disabled` (it knows `admin.id` and can read the active-admin count). This is
UX only — the service + router guards remain the enforcement.

**Templates** under `serve/admin/templates/users/`: `list.html`, `form.html`,
`_form_fields.html`, `_row.html`, `_grants.html`, `_secret_status.html`,
`_delete_confirm.html`, `_delete_blocked.html`.

**Static:** `serve/admin/static/users-panel.js` — minimal (e.g. delete confirm);
served file, no inline JS, CSP `script-src 'self'` (consistent with
`accounts-panel.js`).

### Pure form module — `src/localmail/serve/admin/user_forms.py`

Unit-tested in isolation, like `account_forms.py`:

- `form_to_create_kwargs(form) -> dict` — extract `username`, `password`,
  `is_admin` (checkbox → bool); raise `FormError` on blank username/password.
- `field_errors_from(err) -> dict[str, str]` — map
  `UserFieldError` / `FormError` / `LastAdminError` / `SelfActionError` to
  `{field: message}` (substring-hinted, `_form` fallback) for inline rendering.

## Wiring

Include both routers in `serve/app.py` inside the existing admin-mount block
(gated on `session_signing_key`): `users_panel_router` under `/admin`,
`users_router` under `/v1/admin`. The nav link and the static mount already
exist.

## Testing (TDD — tests first, red → green)

- **`tests/test_api_admin_users.py`** (service, real test DB):
  `list_users` / `get_user` shape incl. `account_grants` accuracy; `create_user`
  incl. `is_admin` + duplicate-username → `UserFieldError`; `set_password` reset
  (login works with new pw, fails with old); `set_disabled` / `set_admin` /
  `delete_user` happy paths; **last-admin guard** — demote/disable/delete the
  sole active admin → `LastAdminError`, allowed when a second active admin
  exists; disabled admins don't count toward the active total; `set_grant`
  grant+revoke idempotence.
- **`tests/test_user_forms.py`** (pure, no DB): `form_to_create_kwargs` parsing
  (checkbox→bool, blanks→`FormError`); `field_errors_from` mapping for each
  error type incl. `_form` fallback; `would_orphan_last_admin` truth table
  (every combination of inputs).
- **`tests/test_serve_admin_users.py`** (JSON `/v1/admin/users`): auth required;
  CSRF required + method-bound (a PATCH token cannot replay on DELETE);
  create → 400 on dup; guard → 409 (last-admin + self); grants round-trip; IDs
  are strings on the wire.
- **`tests/test_serve_admin_user_screens.py`** (HTML `/admin/users`): unauth →
  redirect; list/edit render; create validation → inline error fragment; grant
  checklist toggle swaps `_grants.html`; delete-blocked → `_delete_blocked.html`;
  unsafe controls rendered `disabled`.

Self-action (router-level) guard is tested in both transport suites since it is
enforced there, not in the service.

## Conventions honoured

- No magic numbers; pure functions (`would_orphan_last_admin`, the form module)
  in reusable, independently-tested modules.
- Files kept focused and under ~500 lines (the service composes primitives; the
  routers stay thin; templates carry the markup).
- TDD throughout; inline docs only where the *why* is non-obvious.
- `is_admin IS TRUE` everywhere (nullable column).

## Risks / open items

1. **Last-admin definition = active admins** (`is_admin IS TRUE AND disabled_at
   IS NULL`). A disabled admin does not protect against lock-out — re-enabling
   requires DB/CLI access. Documented; acceptable for v1.
2. **Self-demote via PATCH** is blocked even when other admins exist (it's a
   `SelfActionError`), matching self-delete. Rationale: an admin demoting
   themselves is almost always a mistake; another admin can demote them. Revisit
   if operators report friction.
3. **No new migration** — re-check `ls migrations/` at plan time; if anything
   landed since `0025`, the next free slot shifts but this design needs none.
