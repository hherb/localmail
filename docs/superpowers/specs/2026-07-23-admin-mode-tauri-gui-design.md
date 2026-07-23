# Admin mode in the Tauri desktop app — design

> Adds an **operator/admin mode** to the existing read-only `gui/` Tauri 2 +
> Svelte 5 desktop app, unlocked when the logged-in user is `is_admin`. It
> drives the existing `/v1/admin/*` JSON API over the same pinned-TLS HTTPS
> connection the viewer already uses. v1 target: **full admin parity** —
> Accounts + secrets (incl. Gmail OAuth connect), Daemon control, Users & ACL,
> Archive imports. macOS is the first packaging target (a dmg is already
> configured).
> Date: 2026-07-23. Branch: `feat/admin-mode-gui` (suggested).

## Problem

localmail's admin surface (account CRUD + secrets, Gmail OAuth connect, user &
per-account ACL management, mbox/maildir imports, daemon control) exists today
in exactly two forms:

1. A server-rendered **HTMX web UI** at `/admin/*`, authenticated by an HMAC
   **session cookie** + per-action **CSRF** token (`X-CSRF-Token`).
2. A **JSON API** at `/v1/admin/*` behind the *same* cookie + CSRF gate (the
   JSON routers are what the HTMX panels call).
3. Plus the `localmail` **CLI** for the same operations.

There is no native desktop admin experience. Meanwhile the repo already ships a
mature **read-only desktop viewer** under `gui/` (Tauri 2 + Svelte 5) that talks
to the `serve` HTTPS **read** API as a downstream client, with a reusable Rust
layer: a custom rustls TLS verifier for self-signed certs
([gui/src-tauri/src/http/](../../../gui/src-tauri/src/http/)), keyring token
storage, and a connect → login → session flow.

The gap: an operator on macOS wants to *manage* localmail (add a Gmail account,
connect OAuth, enable/disable sync, watch and nudge the daemon, manage users,
run an import) from an installable native app — not the CLI or a browser
pointed at a self-signed-TLS admin URL.

## Decision summary

Resolved during brainstorming (2026-07-23):

- **App structure:** *Admin mode inside the existing `gui/` app*, gated on
  `is_admin`. One binary, one install; reuses the whole Rust HTTP/keyring/session
  layer. (Rejected: separate binary; thin webview shell over the HTMX admin.)
- **v1 scope:** *Full admin parity* — all four capability groups.
- **Auth model:** *Bearer-token admin auth.* Extend `/v1/admin/*` to also accept
  `Authorization: Bearer <token>` when the token's user is `is_admin`, and skip
  CSRF for bearer-authed requests (a native app sending a bearer token in a
  header is structurally CSRF-immune). The app reuses the viewer's existing
  `/v1/auth/login` → keyring → `Authorization: Bearer` flow verbatim. (Rejected:
  replicating cookie + CSRF in the native client; direct Postgres + `launchctl`
  from Rust.)

The daemon's core invariant is unchanged: **no IMAP write path.** Admin writes
touch the Postgres archive/config and control local processes only.

## Non-goals (v1)

- Managing the launchd agents directly (start/stop the daemon *process* on the
  operator's mac). Under the user's deployment `serve` runs with
  `supervise_daemon = false`, so lifecycle ops return 409; the app degrades
  gracefully (see "Daemon lifecycle" below). Direct launchd management is a
  larger, separate scope.
- A second binary / separate admin identity.
- codesigning / notarization of the dmg (a distribution/ops task, flagged in the
  final phase, not part of the feature).
- Any change to the web HTMX admin's cookie + CSRF behaviour.

## Architecture

```
Operator ── Tauri app (gui/, is_admin mode) ──┐
                                              │  Authorization: Bearer <admin token>
                                              │  (pinned-TLS HTTPS, no cookie, no CSRF)
                                              ▼
                               serve  /v1/admin/{accounts,users,imports,daemon}
                                              │
                                              ▼
                               localmail.api.admin.*  ── Postgres (canonical) + daemon DB queues
```

The app is one more downstream client of `serve`, exactly like the viewer. It
never talks to Postgres or the daemon directly.

## Component design

### Backend (Python) — two additive changes

Both are small, additive, and independently shippable; the web admin is
untouched.

**(a) Expose `is_admin`.**
- Add `is_admin: bool` to `AuthenticatedUser`
  ([src/localmail/api/auth.py](../../../src/localmail/api/auth.py)); populate it
  in `verify_token`'s SELECT by adding `u.is_admin` to the joined columns.
- Add `is_admin: bool` to the `WhoamiResponse` in
  [src/localmail/serve/routes/auth.py](../../../src/localmail/serve/routes/auth.py).
  The app calls `GET /v1/auth/whoami` right after login to decide whether to
  reveal admin nav.

**(b) `require_admin` dependency + conditional CSRF.**
- New dependency (in `serve/admin/dependencies.py` or a sibling) that accepts
  **either** auth:
  - `Authorization: Bearer <token>` → `verify_token` → require `is_admin`
    (else **403**) → return an `AdminUser`; set
    `request.state.admin_auth_kind = "bearer"`.
  - session cookie → the existing `require_admin_session` path unchanged; set
    `request.state.admin_auth_kind = "cookie"`.
  - Neither / invalid → the existing behaviour (cookie path redirects; a bad
    bearer → 401).
- `check_csrf` ([src/localmail/serve/admin/csrf.py](../../../src/localmail/serve/admin/csrf.py))
  early-returns (no-op) when `request.state.admin_auth_kind == "bearer"`. Cookie
  auth still requires and verifies CSRF exactly as today.
- Swap `require_admin_session()` → `require_admin()` in the four JSON routers:
  [accounts_router.py](../../../src/localmail/serve/admin/accounts_router.py),
  [users_router.py](../../../src/localmail/serve/admin/users_router.py),
  [imports_router.py](../../../src/localmail/serve/admin/imports_router.py),
  [daemon_router.py](../../../src/localmail/serve/admin/daemon_router.py).
- The HTML panel routers (`*_panel_router.py`) keep `require_admin_session` and
  cookie + CSRF — the web UI is unchanged.

Security note: bearer admin access is exactly as strong as the session cookie
(both resolve to an `is_admin` user). CSRF is skipped *only* for bearer auth,
which does not carry ambient cookie credentials, so the CSRF threat model does
not apply. The
[tests/test_session_cookie_scope.py](../../../tests/test_session_cookie_scope.py)
invariant (no non-`/v1/admin/*` route reads the session cookie) stays green — we
add a *bearer* path, never a new cookie reader.

Blast-radius note: a token issued to an `is_admin` user is now an *admin*
credential, not merely an ACL-scoped read credential. Any bearer belonging to
an admin user unlocks the full `/v1/admin/*` write surface (user/account CRUD,
imports, daemon control). This is deliberate — it mirrors the session cookie's
authority — but it does mean the leak consequence for that token class is
now admin-write, not just read. There is no per-token scope (no read-only
token for an admin user) and admin mutations are not audit-differentiated by
auth channel (`admin_auth_kind` is consumed only by `check_csrf`). Both are
tracked as follow-ups in #204, not v1 blockers; the mitigation is unchanged
(the token lives only in the OS keyring, same as the viewer's read token).

### Frontend (Svelte)

- `ConnectScreen` + `LoginScreen` reused unchanged. On successful login the app
  calls `whoami`; `session` stores `is_admin`.
- A nav control ("Admin") is rendered only when `is_admin` is true.
- New `screens/AdminView.svelte` hosts four sub-panels, each a focused component
  under `components/admin/` following existing `components/` conventions:
  - **AccountsPanel** — list / create / edit / delete, store password,
    test-connection, enable/disable sync, Gmail **Connect** (OAuth).
  - **DaemonPanel** — status + heartbeats + recent logs (self-refresh), reload,
    per-account restart-sync; lifecycle buttons (start/stop/restart) disabled
    when `supervise_daemon_externally` (see below).
  - **UsersPanel** — create / delete users, grant/revoke per-account ACL,
    `is_admin` toggle, password reset, enable/disable, revoke sessions.
  - **ImportsPanel** — create / list / cancel mbox & maildir jobs with progress.
- Validation errors render inline per-field; conflict/busy/auth errors surface as
  a transient toast. Nothing fails silently.

### Rust command layer

- New `commands/admin/` module: `admin_accounts.rs`, `admin_users.rs`,
  `admin_imports.rs`, `admin_daemon.rs`. Each is a thin proxy over the existing
  `http::client` (pinned TLS verifier + stored bearer token from keyring),
  returning typed JSON to the frontend and reusing `http/errors.rs` status
  mapping (401 → re-login, 403 → not-admin, 400 → validation detail, 409 →
  conflict/busy). Registered in `commands/mod.rs` and the Tauri `invoke_handler`.

## Two real-world flows called out

### Daemon lifecycle under launchd

The operator runs the daemon and `serve` as *separate* launchd agents, so
serve's supervisor is the `ExternalDaemonSupervisor` stub: `start`/`stop`/
`restart` raise `SupervisorUnavailable` → **409**. `GET /v1/admin/daemon`
reports `supervise_daemon_externally = true`. The DaemonPanel therefore:
- **disables** the lifecycle buttons when that flag is set, and
- keeps **status + heartbeats + recent logs** and the **DB-queue controls**
  (reload, per-account restart-sync) working — these operate via the daemon
  command queue regardless of who owns the process.

This mirrors the existing web daemon panel exactly.

### Gmail OAuth connect

Reuse the existing web OAuth flow — no new backend:
1. App calls `POST /v1/admin/accounts/{id}/oauth/start` → receives a Google
   consent URL (serve writes the HMAC-signed state).
2. App opens the consent URL in the **system browser** (Tauri opener/shell).
3. Google redirects to serve's `GET /admin/oauth/callback?state=&code=`; serve
   verifies state, exchanges the code, stores the refresh token in keyring.
4. App **polls the account's secret status** (`GET /v1/admin/accounts/{id}`)
   until the refresh token is present, then shows "connected".

Requires `serve` to be reachable at the redirect URI registered with Google
(satisfied for the local/loopback deployment).

## Error handling

- 401 → token expired/invalid → drop to the login screen (existing viewer
  behaviour).
- 403 → authenticated but not admin → admin nav is never shown, and the API is
  the real gate; surfaced as a clear message if hit.
- 400 → validation → inline per-field messages (the JSON routers already return
  structured field errors).
- 409 → conflict / busy / cascade-refuse / external-supervisor → toast; the UI
  reflects the server's refusal rather than pretending success.

## Testing

- **Python (pytest):**
  - A bearer token for an `is_admin` user drives each of the four `/v1/admin/*`
    routers (read + at least one mutation each).
  - A bearer token for a non-admin user → **403** on every admin router.
  - Bearer-authed mutations succeed **without** an `X-CSRF-Token` header.
  - Cookie-authed mutations still **require and verify** CSRF (regression — the
    conditional skip must not weaken the cookie path).
  - `GET /v1/auth/whoami` returns `is_admin` for both admin and non-admin users.
  - `tests/test_session_cookie_scope.py` stays green.
- **Rust (cargo + mockito):** one unit test per admin command for the happy path
  plus each mapped error status.
- **Svelte (vitest):** one test per admin panel — renders, an action dispatches
  the correct Tauri command, and error states render.

## Implementation order (phased — parity is large)

1. **Backend** (§ "Backend"): `is_admin` on whoami + `require_admin` bearer
   dependency + conditional CSRF + swap into the 4 routers + tests. Ships alone;
   unblocks everything.
2. **Frontend shell:** is_admin detection, admin nav/route, `AdminView` scaffold.
3. **Accounts panel** (+ Gmail OAuth connect) — highest daily value.
4. **Daemon panel** (status/logs + DB-queue controls; lifecycle gated on
   supervision).
5. **Users & ACL panel.**
6. **Imports panel.**
7. **macOS packaging note:** `tauri.conf.json` already builds a dmg;
   **codesigning / notarization** for distribution is a separate ops task,
   flagged not scoped.

## Files touched (anticipated)

Backend: `src/localmail/api/auth.py`, `src/localmail/serve/routes/auth.py`,
`src/localmail/serve/admin/dependencies.py`,
`src/localmail/serve/admin/csrf.py`, and the four `*_router.py` admin routers;
new tests under `tests/`.

Frontend: `gui/src/screens/AdminView.svelte`, `gui/src/components/admin/*`,
`gui/src/lib/api/admin*.ts`, session/nav wiring in the existing router/screens;
vitest siblings.

Rust: `gui/src-tauri/src/commands/admin/*.rs` + registration in
`commands/mod.rs`; cargo tests.

No new migration. No new uv extra. No new Rust dependency (reuses `reqwest` +
the pinned verifier + `keyring`).
