# localmail admin UI — design

**Status:** Approved, ready for implementation planning
**Date:** 2026-05-28
**Author:** Horst Herb, with Claude (brainstorming session)

## Goal

A web-based **admin UI** served by `localmail serve` at `/admin/`, used by
operators (not regular search users) to:

1. **Manage accounts** — add, edit, remove IMAP accounts (password and Gmail
   OAuth2). Run the OAuth consent dance entirely in the browser. List folders,
   toggle `folder_deny_flags`. Store secrets via the existing keyring wrapper.
2. **Control the sync daemon** — start, stop, restart it; show live status
   (per-account IDLE/poll thread state, last heartbeat, recent errors).
3. **Import mbox archives** — upload (small) or pick from a server-side staging
   directory (large); each archive becomes a virtual account with one mailbox
   per file; a background worker streams parses through the existing
   `process_one_message` path so dedup, attachments, FTS, and embedding all
   reuse existing code.

This complements but does not replace the read-only Tauri search GUI shipped
under `gui/` — both are clients of the same `localmail serve` process.

## Non-goals (deliberately scoped out)

- No mail-write actions (still no send, no flag changes, no IMAP-side mutation
  — preserves the existing project-wide invariant).
- No multi-user admin roles. A single `is_admin` boolean on `api_users` is
  enough; roles/groups are not in v1.
- No per-account daemon pause toggle in v1 (whole-daemon only). The
  `accounts.sync_enabled` column lands now but the daemon does not honor it
  yet — that ships in v1.x.
- No O365 / Outlook OAuth. Gmail OAuth + password only, matching today's CLI.
- No Windows **server** support. The subprocess supervisor uses POSIX signal
  semantics. Windows clients (the browser) are fine.
- No audit log of admin actions in v1 (flagged as v1.x; see Future work).
- No resumable / chunked uploads, no `mbox.gz`, no Maildir, no Thunderbird
  profile import. Single uncompressed mbox file at a time.
- No automatic restart on daemon crash. v1 surfaces the crash in the UI; the
  operator decides what to do.

## Architecture

Three logical surfaces inside one FastAPI process, with two optional
subprocess children:

```
                       ┌──────────────────────────────┐
 Operator's browser ───┤ HTTPS + bearer (admin token) │
                       │ HTML+HTMX from Jinja2        │
                       └────────┬─────────────────────┘
                                │  /admin/*  /v1/admin/*
                                ▼
        ┌───────────────────────────────────────────────────┐
        │ localmail serve (FastAPI)                         │
        │ ┌────────────────┐  ┌─────────────────────────┐   │
        │ │ existing /v1/* │  │ /admin/* + /v1/admin/*  │   │
        │ │ (search/etc)   │  │ (Jinja2 + JSON)         │   │
        │ └────────────────┘  └────────┬────────────────┘   │
        │                              │                    │
        │  DaemonSupervisor ◄──────────┤ start/stop/restart │
        │     │ subprocess.Popen       │                    │
        │     ▼                        │                    │
        │  localmail run               │                    │
        │   (child process)            │                    │
        │                              │                    │
        │  ImportWorkerSupervisor ◄────┤ enqueue mbox job   │
        │     │ subprocess.Popen       │                    │
        │     ▼                        │                    │
        │  localmail import-worker     │                    │
        │   (child process, polls      │                    │
        │    import_jobs table)        │                    │
        └────────────────┬─────────────┴────────────────────┘
                         │
                  PostgreSQL (canonical state)
```

### New invariants (to be added to CLAUDE.md once shipped)

- **`localmail.api.admin`** is the canonical service-layer module for every
  admin action — account CRUD, daemon control, mbox imports. `localmail.serve.admin`
  is a thin Jinja2 + HTMX wrapper over it. A future MCP-admin or scripting
  layer can import the service directly without going through HTTP.
- **DB is canonical for accounts.** `config.toml` `[[accounts]]` blocks are a
  one-time seed merged into the `accounts` table on `init-db`; thereafter the
  DB is authoritative. The daemon, CLI, and admin UI all read accounts from
  the DB. This is a deliberate departure from the v1 model where TOML was
  authoritative; the new model is necessary for runtime account add/remove.
- **`serve` is the optional supervisor** for `localmail run` and the import
  worker. Opt-out via `[serve] supervise_daemon = false` and
  `[serve] supervise_import_worker = false` for systemd/launchd operators.
  Data flow stays Postgres-only — the new coupling is a control plane, not a
  data plane.
- **Admin UI uses cookie-session auth.** Bearer-token auth remains the only
  path for `/v1/*` machine clients (Tauri client, MCP server, scripts). The
  cookie path is scoped to `/admin` and exists because OAuth callback
  redirects cannot carry a bearer header.

## 1. Schema additions

Four new migrations, numbered after the current latest
(`0019_api_login_attempts.sql`):

### `0020_accounts_canonical.sql`

Promote `accounts` to be authoritative. Adds the fields currently held in
`config.toml`:

```sql
ALTER TABLE accounts
  ADD COLUMN imap_host           TEXT,
  ADD COLUMN imap_port           INT,
  ADD COLUMN auth_method         TEXT NOT NULL DEFAULT 'password'
             CHECK (auth_method IN ('password', 'oauth2', 'archive')),
  ADD COLUMN oauth_provider      TEXT
             CHECK (oauth_provider IS NULL OR oauth_provider IN ('gmail')),
  ADD COLUMN folder_allow        JSONB,
  ADD COLUMN folder_deny         JSONB,
  ADD COLUMN folder_deny_flags   JSONB,
  ADD COLUMN sync_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN updated_at          TIMESTAMPTZ NOT NULL DEFAULT now();

-- live IMAP accounts must have host + port; archive accounts must not.
ALTER TABLE accounts
  ADD CONSTRAINT accounts_live_requires_host
  CHECK (
    (auth_method = 'archive' AND imap_host IS NULL AND imap_port IS NULL)
    OR (auth_method IN ('password', 'oauth2') AND imap_host IS NOT NULL AND imap_port IS NOT NULL)
  );
```

A one-time TOML→DB merge runs at the top of `init-db` (idempotent, keyed by
`accounts.name`). After 0020 has applied, TOML changes to `[[accounts]]` are
ignored at runtime; operators get a WARNING log if they edit TOML after the
DB has been populated.

`sync_enabled` is reserved for future per-account pause; v1 daemon does not
check it (every row spawns IDLE+poll threads as today). Landing the column
now avoids a follow-on migration.

### `0021_api_users_admin.sql`

```sql
ALTER TABLE api_users
  ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX api_users_is_admin_idx ON api_users (is_admin) WHERE is_admin;
```

Bootstrap is shell-only: `localmail grant-admin USERNAME` flips it from the
server host. Non-admin authenticated users hitting `/admin/*` get **403**, not
404 — the surface is operator-facing and there is no value in hiding its
existence.

### `0022_import_jobs.sql`

```sql
CREATE TABLE import_jobs (
  id              BIGSERIAL PRIMARY KEY,
  source_kind     TEXT NOT NULL CHECK (source_kind IN ('upload', 'server_path')),
  source_uri      TEXT NOT NULL,
  account_id      INT  NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  mailbox_id      INT  NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
  state           TEXT NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  progress_total  BIGINT,
  progress_done   BIGINT NOT NULL DEFAULT 0,
  error_class     TEXT,
  error_msg       TEXT,
  error_traceback TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ,
  created_by      INT REFERENCES api_users(id)
);
CREATE INDEX import_jobs_queue_idx ON import_jobs (state, created_at)
  WHERE state IN ('queued', 'running');
```

The partial index is what the worker uses for `FOR UPDATE SKIP LOCKED` polling.

### `0023_daemon_heartbeats.sql`

```sql
CREATE TABLE daemon_heartbeats (
  account_id        INT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  thread_kind       TEXT NOT NULL CHECK (thread_kind IN ('idle', 'poll')),
  last_heartbeat_at TIMESTAMPTZ NOT NULL,
  current_folder    TEXT,
  state             TEXT NOT NULL CHECK (state IN ('connecting', 'idle', 'polling', 'syncing', 'error', 'reconnecting')),
  last_error_msg    TEXT
);
```

The daemon's per-account threads update their row at the top of each loop
iteration. The supervisor reads this for "is it actually doing work?" status
rather than relying on PID-alive checks.

**No `oauth_pending` table.** OAuth state is HMAC-signed and stateless (see
Section 2A below).

## 2A. Account management

### Service layer

`src/localmail/api/admin/accounts.py`:

```python
def list_accounts(*, allowed_admin: bool) -> list[AccountSummary]: ...
def get_account(account_id: int) -> Account: ...
def create_account(*,
                   name: str, email: str | None,
                   imap_host: str | None, imap_port: int | None,
                   auth_method: Literal['password', 'oauth2', 'archive'],
                   oauth_provider: str | None,
                   folder_allow, folder_deny, folder_deny_flags,
                   created_by: int) -> Account: ...
def update_account(account_id: int, **fields) -> Account: ...
def delete_account(account_id: int, *, force: bool = False) -> None: ...
def store_password(account_id: int, password: str) -> None: ...
def clear_secret(account_id: int) -> None: ...
def test_connection(account_id: int) -> list[FolderInfo]: ...
```

Field validation lifts the existing `AccountConfig` pydantic model out of
`config.py` into a shared validator so daemon + admin agree on field rules.

`delete_account` refuses when `messages` rows reference the account, unless
`force=True`. Cascade deletes through `message_labels`, `mailboxes`,
`failed_messages`, etc. Keyring secrets cleared atomically (best-effort:
keyring failure logs a WARNING but does not roll back the DB delete; orphaned
keyring entries are harmless).

`test_connection` opens an IMAP connection, lists folders, returns names. The
UI requires this to succeed before declaring the account ready.

### OAuth flow (HMAC-signed state, stateless)

`src/localmail/api/admin/oauth.py`:

```python
def start_oauth(account_id: int, *, admin_user_id: int) -> str:
    """Return the Google consent URL with a signed state token."""
    payload = {
        "user_id": admin_user_id,
        "account_id": account_id,
        "nonce": secrets.token_urlsafe(16),
        "exp": int(time.time()) + 300,    # 5 min
    }
    state = _sign(payload, key=cfg.serve.state_signing_key)
    return _build_google_consent_url(state=state, redirect_uri=cfg.serve.oauth_callback_url)

def complete_oauth(state: str, code: str, *, admin_user_id: int) -> Account:
    payload = _verify_and_decode(state, key=cfg.serve.state_signing_key)
    if payload["user_id"] != admin_user_id:
        raise PermissionDenied()
    if payload["exp"] < time.time():
        raise StateExpired()
    refresh_token = _exchange_code_with_google(code)
    _store_refresh_token(payload["account_id"], refresh_token)
    return get_account(payload["account_id"])
```

Token format: `base64url(json(payload)) + "." + base64url(hmac_sha256(key, base64url(json(payload))))`.

Threat-model rationale (chosen over a DB-backed `oauth_pending` table):

- **DB compromise yields nothing**: the secret is the HMAC key in serve's
  config/memory. There is no row to leak.
- **Replay-within-TTL is bounded** by Google's own single-use guarantee on
  `code` — the second exchange against the same code fails at Google.
- **Cross-user completion is blocked**: the payload includes the initiating
  admin's user_id, checked against the currently-authenticated cookie session.
- **No sweeper, no TTL housekeeping** — expiry is a signature check.

Required config:

```toml
[serve]
state_signing_key = "..."     # 32 random bytes (secrets.token_urlsafe(32))
oauth_callback_url = "https://<server>/admin/oauth/callback"
```

If `state_signing_key` is missing at startup, serve **fails loudly** (exit
1, with a hint to run `secrets.token_urlsafe(32)`). Auto-generation is not
acceptable because a regenerated key invalidates in-flight admin sessions
silently. Same rule applies to `session_signing_key` (see Section 3).

The callback URL must be registered in Google Cloud Console as an authorized
redirect URI. The existing CLI desktop loopback flow stays in place for
shell-only operators (no breakage).

PKCE is **not** in v1. The `client_secret` provides confidential-client auth
already, and Google's `code` is single-use. PKCE is documented as future
hardening in the Future work section.

### HTTP shape

```
GET    /v1/admin/accounts                       # list
POST   /v1/admin/accounts                       # create
GET    /v1/admin/accounts/{id}                  # detail
PATCH  /v1/admin/accounts/{id}                  # partial update
DELETE /v1/admin/accounts/{id}?force=true|false # remove
POST   /v1/admin/accounts/{id}/password         # store password (JSON {password})
POST   /v1/admin/accounts/{id}/oauth/start      # → {auth_url}
GET    /admin/oauth/callback?state=…&code=…     # Google redirect target
POST   /v1/admin/accounts/{id}/test-connection  # → {folders: [...]}
```

`/admin/oauth/callback` is the only admin path served as HTML; it 302s back
to `/admin/accounts/{id}?oauth=success|failed`.

## 2B. Daemon control

### `DaemonSupervisor`

`src/localmail/serve/daemon_supervisor.py`:

```python
class DaemonSupervisor:
    """Owns the lifecycle of `localmail run` as a child process.

    State machine:
      stopped → starting → running → stopping → stopped
                                  └→ crashed (on non-zero exit)
    """
    def start(self) -> None: ...
    def stop(self, timeout: float = 30.0) -> None: ...
    def restart(self) -> None: ...
    def status(self) -> SupervisorStatus: ...
    def recent_log_lines(self, n: int = 40) -> list[str]: ...
```

- Created at serve startup **only if** `cfg.serve.supervise_daemon` is true
  (default true). When false the supervisor is a stub that always reports
  state `external` and refuses start/stop calls — for systemd/launchd.
- Spawns `[sys.executable, "-m", "localmail.cli", "run"]` via
  `subprocess.Popen`. Captures stdout/stderr to the
  `localmail.serve.daemon_supervisor` logger plus a bounded ring buffer
  (last 200 lines) for UI display.
- `stop()` sends SIGTERM, waits up to `daemon.shutdown_grace_seconds`, then
  SIGKILL.
- **Health signal**: reads `daemon_heartbeats` for per-account staleness.
  `state="error"` rows surface in the UI; staleness past
  `daemon.heartbeat_stale_seconds` (default 120s) marks a thread "stalled"
  even if the PID is alive.
- **No auto-restart in v1.** A crash sets state to `crashed`, surfaces the
  last 40 stderr lines + exit code in the UI. v1.x can add exponential
  backoff restart.
- **Listens on a local Unix socket** at `${runtime_dir}/localmail-supervisor.sock`
  so the CLI (`localmail daemon status/start/stop/restart`) can talk to a
  running serve. The socket is `0600`, owned by the serve user.

### HTTP shape

```
GET  /v1/admin/daemon                # {state, pid, started_at, heartbeats: [...], recent_log: [...]}
POST /v1/admin/daemon/start
POST /v1/admin/daemon/stop
POST /v1/admin/daemon/restart
```

`GET /v1/admin/daemon` includes `supervise_daemon_externally: bool`. When
true, the UI disables start/stop buttons and shows
"Daemon is supervised externally (systemd/launchd). Use that to start or
stop. Status below is read-only."

## 2C. mbox import

### Mapping (chosen)

Each imported archive becomes a new `accounts` row with:

- `auth_method = 'archive'`
- `imap_host = NULL`, `imap_port = NULL` (enforced by check constraint
  from 0020)
- `name` operator-supplied (e.g. `legacy-fastmail-2017`)
- `email` optional

Each mbox file becomes one `mailboxes` row under that account, with:

- `name` = the mbox filename (e.g. `INBOX.mbox` → `INBOX`)
- `uidvalidity = 0`, `uidnext = 0` (sentinels; archive mailboxes are never
  synced — the `NOT NULL` constraints on existing columns are satisfied
  without functional impact)

Dedup semantics:

- Same Message-Id appearing in both a live account and an imported archive
  → **two separate rows** (preserves provenance; matches today's
  account-scoped Message-Id dedup).
- Same Message-Id appearing twice inside the same mbox → **one row** (the
  second insert is a no-op via the existing `ON CONFLICT DO NOTHING`).
- mbox messages without Message-Id → dedup by raw SHA-256 (matches today's
  fallback rule).

### Two delivery flows

**HTTPS upload** (small files, gated by `[import] upload_max_bytes`,
default 500 MB):

- `POST /v1/admin/imports/upload` — `multipart/form-data` body.
- Streams to a temp file under `[import].staging_root` (default
  `~/localmail/import-staging/`).
- Enforces `upload_max_bytes` *while streaming* via FastAPI's request size
  limit + a manual byte counter — never trusts the `Content-Length` header.
- On completion, enqueues an `import_jobs` row with
  `source_kind='upload'`, `source_uri=<temp_path>`.
- Temp file is deleted after the worker finishes (success or failure).

**Server-side path** (large files, unlimited size):

- `POST /v1/admin/imports/server-path` with JSON `{path, account_name,
  mailbox_name, email}`.
- The path **must resolve under** `[import].server_root` (default
  `~/localmail/import-source/`). Symlinks dereferenced before the prefix
  check; anything outside is rejected with 400.
- `GET /v1/admin/imports/server-files` walks `server_root` for `.mbox`
  files and returns sizes + mtimes for a UI picker.
- Source files are **kept** after import — operators remove them manually.
  This is the legacy-archive use case; deleting "decades of mail from a
  dead server" by accident would be catastrophic.

### Import worker

New module `src/localmail/import_worker.py` and new CLI subcommand
`localmail import-worker`:

```python
def run_worker_forever(cfg: Config) -> None:
    """Poll import_jobs, process one at a time, LISTEN for new jobs."""

def process_one_job(conn, job_id: int) -> None:
    """Open the mbox, iterate, parse, upsert. Same SAVEPOINT-per-message
    discipline as live sync — poison messages land in failed_messages."""
```

- Poll: `SELECT … FROM import_jobs WHERE state='queued' ORDER BY created_at
  LIMIT 1 FOR UPDATE SKIP LOCKED` — only one worker process picks each job
  even if multiple race.
- For each message: pass the raw RFC822 bytes through the existing parser
  + `process_one_message(conn, parsed, account_id, mailbox_id, …)`.
- **SAVEPOINT per message** identical to live sync — poison messages
  recorded in `failed_messages` and skipped.
- `progress_done` updated every `[import] progress_update_every` messages
  (default 500).
- **Cancellation**: every batch boundary checks `state` — if it flipped to
  `cancelled`, the worker rolls back its in-flight batch, sets
  `finished_at`, and moves to the next job.
- On completion: `state='succeeded'`, `finished_at=now()`. Uploaded source
  file deleted; server-path source file left in place.
- On unhandled exception: `state='failed'`, `error_class`, `error_msg`,
  `error_traceback` populated.
- **Idle behaviour**: after no queued job is found, the worker issues
  `LISTEN import_jobs_queued` and blocks until NOTIFY arrives or a
  60-second poll deadline elapses (defense against missed NOTIFYs from
  short-lived connections). FastAPI's `POST` enqueue path sends
  `NOTIFY import_jobs_queued` after `INSERT … RETURNING id`.

### `ImportWorkerSupervisor`

`src/localmail/serve/import_worker_supervisor.py`:

- Same supervisor shape as `DaemonSupervisor`.
- Default-on; opt-out via `[serve] supervise_import_worker = false` for
  operators who want to run `localmail import-worker` under systemd/launchd
  themselves.
- Single worker process at a time. Multi-job parallelism is out of scope
  for v1 (see Future work).
- Worker exits cleanly when serve sends SIGTERM (after finishing the
  current message); supervisor respawns on serve restart.

### HTTP shape

```
POST /v1/admin/imports/upload                # multipart, → 202 {job_id}
POST /v1/admin/imports/server-path           # JSON, → 202 {job_id}
GET  /v1/admin/imports/server-files          # → {files: [{path, size, mtime}, ...]}
GET  /v1/admin/imports                       # paginated job list, ?state=…
GET  /v1/admin/imports/{job_id}              # → {state, progress, error, ...}
POST /v1/admin/imports/{job_id}/cancel       # → 202
```

## 3. Authentication & authorization

### Two auth paths

**Bearer-token auth** (machine clients on `/v1/*`) — unchanged. Tokens
remain SHA-256 hashed in `api_tokens`.

**Cookie-session auth** (admin browser UI on `/admin/*` and
`/v1/admin/*`) — new, scoped to `/admin`.

- `POST /admin/login` (form-encoded `username`, `password`) — argon2
  verified via the existing path. Issues a signed session cookie.
- Cookie attributes:
  - `HttpOnly`, `Secure`, `SameSite=Lax` (Lax not Strict so the OAuth
    callback redirect carries the cookie).
  - `Path=/` — required so the cookie reaches `/v1/admin/*` routes. SameSite=Lax + per-route CSRF tokens are the primary CSRF defenses. No `/v1/*` machine endpoint reads cookies (machine clients use bearer auth on `Authorization:`), so the broader scope adds no smuggling surface. (Sub-plan 2A erratum — original design intent was `/admin` but that's incompatible with the `/v1/admin/*` URL contract.)
  - 8-hour default lifetime; sliding-window renewal on each request.
  - Payload: HMAC-signed `{user_id, issued_at, exp}`. Signed with a
    **separate key** from the OAuth state key:
    `[serve] session_signing_key` (also 32 random bytes). Separate keys
    keep failure domains distinct.
- `POST /admin/logout` — `Set-Cookie` with `Max-Age=0`.

Admin routes resolve cookie → user → check `is_admin = TRUE`. Non-admin
authenticated users get **403**. Anonymous requests redirect to
`/admin/login`.

### Login rate limiting

Reuses the existing Postgres-backed `api_login_attempts` table (migration
`0019`). Same global/per-IP/per-user caps. Same trusted-proxy peeling.
Successful and failed admin logins both register here, alongside `/v1/*`
bearer logins.

### CSRF

Every non-GET admin form includes a CSRF token (HMAC of `(session_id,
form_action)`) injected by the Jinja2 layout template:

- Template macro `{{ csrf_input() }}` for HTML forms.
- HTMX picks it up automatically via `hx-headers='{"X-CSRF-Token": "{{ csrf_token() }}"}'`
  set on the root `<body>`.
- API endpoints under `/v1/admin/*` accept the token in the `X-CSRF-Token`
  header. JSON requests from machine clients carrying a bearer token also
  satisfy CSRF (bearer presence == out-of-band proof).

### Access-log scrubbing

A FastAPI middleware in `serve/middleware.py` redacts `code`, `state`, and
`password` from any logged URL/query string. The cookie itself is never
logged.

### Admin bootstrap (shell-only)

- New CLI: `localmail grant-admin USERNAME` / `localmail revoke-admin
  USERNAME`. Flips `api_users.is_admin`. Must run on the server host.
- `localmail add-api-user --admin` flag for one-step bootstrap of the
  first admin.

## 4. UI structure (Jinja2 + HTMX)

Templates at `src/localmail/serve/admin/templates/`:

```
templates/
  base.html               # layout: nav, flash messages, CSRF helper macros
  login.html
  dashboard.html          # daemon status panel, recent imports, account count
  accounts/
    list.html
    new.html              # account-add wizard step 1 (kind: password / oauth)
    edit.html             # folder denylist edit, secret rotation
    oauth_pending.html    # "we redirected you to Google — waiting for callback"
  daemon/
    panel.html            # start/stop/restart + status table
  imports/
    list.html             # job table with progress bars (htmx-polled while running)
    new.html              # picker: upload vs server-path
    detail.html           # one job's status + tail of error log
  users/
    list.html             # api_users table; grant/revoke admin + per-account ACL
```

- Each route is a thin Jinja2 render. Status panels (daemon state, import
  progress) are partial HTML fragments served at `/admin/_partials/*` and
  polled via `hx-get` + `hx-trigger="every 2s"`.
- No client-side router; URL paths map 1:1 to template renders.
- Single shared CSS at `src/localmail/serve/admin/static/admin.css`. No
  build step, no `node_modules`.
- HTMX vendored at `static/htmx.min.js` (no CDN dependency at runtime).

## 5. CLI parity

Every UI action also has a CLI command — admin actions must remain
shell-accessible for disaster recovery when serve is down:

- `localmail import-mbox PATH [--account NAME] [--mailbox NAME] [--wait]`
  — enqueues a job and prints its ID. `--wait` tails progress and exits
  when the job terminates.
- `localmail daemon {status,start,stop,restart}` — talks to the supervisor
  via the Unix socket at `${runtime_dir}/localmail-supervisor.sock`. When
  `supervise_daemon = false`, prints the external-supervisor note and
  exits non-zero on start/stop/restart.
- `localmail grant-admin USERNAME` / `localmail revoke-admin USERNAME`.
- `localmail add-api-user --admin`.
- Existing `add-account`, `oauth-login`, `remove-account` now write to
  the DB. The daemon and CLI both read from DB after migration 0020.

## 6. Testing

- **Service-layer tests**:
  `tests/test_admin_accounts.py`, `test_admin_oauth.py`,
  `test_admin_imports.py`, `test_admin_daemon_supervisor.py`. Exercise the
  `localmail.api.admin.*` modules directly against a real Postgres test
  DB. Same pattern as the existing `tests/test_api_*.py`.
- **HTTP-route tests**: `tests/test_serve_admin_*.py`. FastAPI TestClient
  against the admin routes, including CSRF + cookie-session paths.
- **Subprocess supervisor tests**: `tests/test_daemon_supervisor.py` and
  `tests/test_import_worker_supervisor.py` spawn tiny dummy subprocesses
  (e.g. a `sleep 300` shim) and verify start/stop/restart/crash detection.
  The real `localmail run` is only exercised in a smoke test
  (`tests/test_admin_smoke.py`, skipped unless an integration env var is
  set) because it needs an IMAP source.
- **mbox import golden tests**: `tests/_mbox.py` builds synthetic mbox
  bytes (like `tests/_eml.py` does for IMAP). End-to-end against the test
  DB; assert dedup behaviour and `failed_messages` recovery on poison
  entries.
- **HMAC token tests**: encode/decode round-trip, expiry, tamper
  detection (flip one bit, verify fails), cross-user replay rejection.
- **No real Google OAuth in CI**. `oauth_gmail` gets a `FakeGoogleOAuth`
  test double, mirroring `FakeIMAPClient` in `tests/_fake_imap.py`.

## 7. Future work

Items deliberately deferred:

- **Per-account daemon pause** — `accounts.sync_enabled` column lands in
  0020 but the daemon does not honor it. v1.x.
- **Multi-job parallel imports** — single worker in v1; multi-worker
  needs a job-claim mechanism + resource budgeting. v1.x or v2.
- **Resumable / chunked uploads** — retry from scratch in v1. v2 if
  someone complains.
- **mbox autodiscovery beyond a single `[import].server_root`** — no
  `.mbox.gz`, no Maildir, no Thunderbird-profile import.
- **Auto-restart on daemon crash** — exponential backoff restart with a
  cap. v1.x.
- **Admin audit log** — `admin_audit_log(actor_user_id, action, target,
  timestamp, request_ip)`. One-table migration, easy add when needed.
- **PKCE** for the Gmail OAuth flow as defense in depth. v1 relies on
  Google's single-use `code` + the confidential `client_secret`.
- **Windows server support** for the subprocess supervisor (POSIX signal
  semantics need a Windows alternative).
- **Migration tool** to convert TOML-only operators to DB-canonical mode
  with a one-shot CLI command (the implicit init-db merge covers the
  common case; an explicit `localmail migrate-config-accounts` may be
  useful for partial migrations).
