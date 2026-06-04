# Sub-plan 2A.3 — Account CRUD admin screens (design)

**Status:** approved 2026-06-04. Successor to the account work in
[2026-05-28-admin-ui-design.md](2026-05-28-admin-ui-design.md). Implements the
HTML UI on top of the already-shipped `api/admin/accounts` service layer and
`/v1/admin/accounts` JSON routes.

## Problem

The admin nav (`base.html`) links `/admin/accounts`, `/admin/imports`, and
`/admin/users`, but only `/admin/` (dashboard) and `/admin/daemon` exist as
HTML. Clicking **Accounts** 404s. The OAuth callback
(`GET /admin/oauth/callback`) already redirects to
`/admin/accounts/{id}?oauth=success` — a route that does not yet exist.

Everything *under* the UI is done: the `api/admin/accounts` service
(`list_accounts`, `get_account`, `create_account`, `update_account`,
`delete_account`, `store_password`, `clear_secret`, `probe_connection`), the
`/v1/admin/accounts` JSON CRUD routes, and the Gmail web-OAuth flow
(`start_oauth`/`complete_oauth`). 2A.3 is the **HTML screens** that drive them.

## Scope

**In scope:** the `/admin/accounts` screens — list, create, edit, delete,
store-password, test-connection, enable/disable sync, and the Gmail OAuth
"Connect" affordance. Plus one backend change: wire `probe_connection` to work
for `oauth2` accounts (today it refuses them).

**Out of scope:** `/admin/imports` and `/admin/users` (separate future
sub-plans); the interactive "pick folders from a live IMAP tree" editor
(deferred follow-up — see Decisions); any change to the `/v1/admin/accounts`
JSON contract beyond threading Gmail secrets into its test-connection route.

## Decisions (from brainstorming)

1. **Scope = accounts only.** The other two dead nav links are future work.
2. **OAuth2 probe is wired in.** `probe_connection` will mint an XOAUTH2 token
   and list folders for Gmail accounts, not refuse them.
3. **Interaction architecture = server-rendered HTMX partials** (Approach A).
   New `/admin/accounts/*` HTML routes render Jinja fragments; forms POST to
   them; validation errors render **inline beside the offending field**; the
   `/v1/admin/accounts` JSON API stays untouched for machine/MCP clients. Both
   layers call the same `api/admin/accounts` service, so there is no logic
   duplication — only a thin second transport.
   - Rejected B (JSON API + client-side JS rendering): a JS rendering blob in a
     Python/TDD codebase, duplicates validation, hard to unit-test, diverges
     from the server-rendered house style.
   - Rejected C (daemon-panel clone: POST to `/v1` JSON + `hx-swap="none"` +
     toast): a JSON `400` is a single string, so multi-field form errors would
     surface as a toast, not beside the field. Worse form UX.
4. **Folder filters = plain text + flag checkboxes** (Option 1). `folder_allow`/
   `folder_deny` are newline-split textareas; `folder_deny_flags` is a fixed
   RFC 6154 checkbox set bound to a module constant. The live-folder-picker is a
   clearly-scoped follow-up once basic CRUD lands.
5. **Method-bound CSRF** via `csrf_token_for_method` + header `check_csrf` —
   the explicit closure of #125, following the latest (daemon-panel, 2B.5)
   pattern rather than the older hidden-field path-only forms.
6. **Successful create/edit redirects** (`HX-Redirect`) to the edit page rather
   than swapping a success banner — gives a stable URL and matches the
   OAuth-callback landing.

## Routes (HTML, mounted at `/admin`)

A new `serve/admin/accounts_panel_router.py`, sibling of
`daemon_panel_router.py`. HTML forms POST only (no PATCH/DELETE verbs);
responses are full pages, HTMX-swapped fragments, or `HX-Redirect`.

| Method · Path | Purpose | Response |
|---|---|---|
| `GET /accounts` | List page | `accounts/list.html` |
| `GET /accounts/new` | Blank create form | `accounts/form.html` (create mode) |
| `POST /accounts` | Create | ok → `HX-Redirect` `/accounts/{id}`; invalid → `_form_fields.html` + inline errors (400) |
| `GET /accounts/{id}` | Edit form (**also OAuth callback landing**) | `accounts/form.html` (edit mode); reads `?oauth=success\|failed` |
| `POST /accounts/{id}` | Update | same error handling as create |
| `POST /accounts/{id}/password` | Store IMAP password (password-auth only) | `_secret_status.html` |
| `POST /accounts/{id}/test-connection` | Probe IMAP, list folders | `_test_result.html` (folders or error) |
| `POST /accounts/{id}/sync-toggle` | Enable/disable sync | swaps `_row.html` |
| `POST /accounts/{id}/delete` | Delete | in-use (409) → confirm-force fragment; `force=1` → `HX-Redirect` `/accounts` |
| `POST /accounts/{id}/oauth/start` | Begin Gmail consent (HTML variant) | `303` redirect to Google `auth_url` |

The existing `/v1/admin/accounts` JSON routes are unchanged except that the
JSON `test-connection` route now also passes
`request.app.state.gmail_client_secrets_file` (so JSON clients gain oauth2
probe too).

## Components

### `serve/admin/accounts_panel_router.py` (~230 lines, thin)

Render + dispatch only. Each route: resolve admin session, open a pool
connection, call the `api/admin/accounts` service, translate domain
exceptions (`NotFound` → 404, `AccountFieldError` → re-render with field
errors / 400, `AccountInUse` → confirm-force fragment / 409), and render a
template or `HX-Redirect`. No business logic.

### `serve/admin/account_forms.py` (~120 lines, PURE — no IO)

All form logic, unit-tested in isolation:

- `DENY_FLAGS: tuple[str, ...]` — the fixed RFC 6154 special-use set
  (`\Trash \Junk \All \Drafts \Sent \Important \Flagged`). No magic strings
  elsewhere.
- `parse_lines(text: str) -> list[str] | None` — split a textarea on newlines,
  strip, drop blanks; empty → `None` (the "no filter" sentinel the DB uses).
- `parse_deny_flags(selected: list[str]) -> list[str]` — keep only members of
  `DENY_FLAGS`; reject unknown.
- `form_to_create_kwargs(form) -> dict` / `form_to_patch_fields(form) -> dict` —
  map raw form values to the service's keyword arguments.
- `account_to_form_values(account) -> dict` — inverse, for prefilling the edit
  form (lists joined back to newline text, flags to a checked-set).
- `field_errors_from(err: AccountFieldError) -> dict[str, str]` — map a service
  error to the offending field name for inline display (falls back to a
  form-level error when no specific field matches).

### Templates (`serve/admin/templates/accounts/`)

- `list.html` — extends `base.html`; table of accounts (name, email, auth,
  sync state, secret state) + "New account"; each row is `_row.html`.
- `_row.html` — one account row; the sync-toggle swap target.
- `form.html` — shared create/edit; auth-method `<select>` drives which field
  groups show; includes `_form_fields.html`.
- `_form_fields.html` — the field block re-rendered on validation error (carries
  `field_errors`); HTMX swap target.
- `_test_result.html` — folder list (name + flags) or error.
- `_secret_status.html` — password/secret status fragment.

### `serve/admin/static/accounts-panel.js` (served static, CSP-safe)

Auth-method field toggle (show/hide host/port/password vs oauth_provider/
Connect-Gmail vs archive) and delete-confirm affordance. Served file, never
inline / never `hx-on::` — `script-src 'self'` (the #148 constraint).

### Backend change: `api/admin/accounts.py`

- `_open_imap_connection(account, *, gmail_client_secrets: Path | None = None)`
  threads the secrets path into the existing
  `imap_client.open_connection(..., gmail_client_secrets=...)` (which already
  supports XOAUTH2).
- `probe_connection(conn, account_id, *, gmail_client_secrets: Path | None =
  None)` drops the `oauth2` refusal and passes the path through. A missing
  refresh token surfaces as a clean `AccountFieldError`
  ("no Google authorization stored — Connect Gmail first"), never a 500.
  Archive accounts are still refused.

## Form behaviour & validation

- **Server-authoritative validation.** The form `hx-post`s; on
  `AccountFieldError` the router re-renders `_form_fields.html` with
  `field_errors` + HTTP 400, HTMX swaps it into the form, errors appear inline.
  No client-side validation duplication.
- **Conditional fields** via `accounts-panel.js`: `password` → host/port/
  password; `oauth2` → oauth_provider + Connect-Gmail; `archive` → neither, no
  test button. Degrades gracefully without JS (all groups visible).
- **Password** is a separate POST (keyring write, not a DB column) → bumps
  `updated_at` via `touch_account_updated_at` so the daemon hot-reload notices.

## OAuth flow

`POST /accounts/{id}/oauth/start` is a CSRF-protected form submit that calls
`svc.start_oauth(...)` and returns `303` straight to Google. The **existing**
`GET /admin/oauth/callback` already persists the refresh token and redirects to
`/accounts/{id}?oauth=success` — no callback change. The edit page reads the
`?oauth=success|failed` query and shows a flash.

## Security

- **Method-bound CSRF** (#125): each mutating control mints
  `csrf_token_for_method("POST", "/admin/accounts/…")` into a per-element
  `hx-headers='{"X-CSRF-Token": …}'` (overriding `base.html`'s body-wide
  default), verified by `check_csrf` (binds `POST:<path>`). Cross-method/
  cross-action replay rejected.
- **CSP:** all JS is served static (`script-src 'self'`).
- **Auth gating:** every route depends on `require_admin_session()`; non-admin/
  expired → redirect to `/admin/login`. Routes live under `/admin/*`, so the
  `/v1` cookie-scope invariant (`test_session_cookie_scope.py`) is unaffected.
- **No new migration**; reuses `sync_enabled` (0020) + the existing service.

## Testing (TDD)

1. **Pure unit tests first** for `account_forms.py`: line-splitting (CRLF,
   blanks, trim), flag parsing (valid-only, unknown rejected), create/patch
   kwargs mapping, prefill inverse, `field_errors_from` per `AccountFieldError`.
2. **Route tests** (FastAPI TestClient + admin cookie + `memory_keyring` +
   `db_conn`): list renders all accounts; create happy-path + each validation
   error inline; edit prefills; delete cascade-or-refuse (409 → confirm →
   force); sync-toggle swaps row; password store; test-connection fragment
   (mock `probe_connection`); oauth/start 303 (mock `svc.start_oauth`).
3. **Security tests:** missing/invalid CSRF → 400; cross-method token replay
   rejected; non-admin → login redirect; assert no inline `<script>` (CSP).
4. **Backend test:** `probe_connection` oauth2 path returns folders (mock
   `_open_imap_connection`) and gives a clean `AccountFieldError` when no
   refresh token is stored.

## Conventions

- Files under 500 lines: the router stays thin by pushing all logic into the
  pure `account_forms.py`. If the router approaches the limit, split the
  test-connection/oauth handlers into a second module.
- No magic numbers / no magic strings: `DENY_FLAGS` is the single source for
  the flag set; no inline flag literals in templates or router.
- No comments unless the WHY is non-obvious.

## Acceptance criteria

- Visiting `/admin/accounts` lists every account with name, email, auth method,
  sync state, and secret state; "New account" reaches a blank form.
- Create/edit forms validate server-side and show inline per-field errors;
  successful submit redirects to the edit page.
- Password accounts: store a password and run test-connection (folder list).
- Gmail accounts: "Connect Gmail" runs the OAuth flow end-to-end (start →
  Google → callback → token stored → `?oauth=success`), and test-connection now
  lists folders for them too.
- Enable/disable sync toggles `sync_enabled` and swaps the row.
- Delete refuses when messages reference the account and offers a force-confirm.
- Every mutating control carries a method-bound CSRF token; missing/invalid/
  replayed tokens are rejected (400). All JS is served static. Non-admins are
  redirected to login.
- `uv run pytest` and `uv run mypy src/localmail` are green; no new migration.
