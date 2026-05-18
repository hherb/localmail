# localmail GUI — design

**Status:** Approved, ready for implementation planning
**Date:** 2026-05-17
**Author:** Horst Herb, with Claude (brainstorming session)

## Goal

A cross-platform compiled desktop application that is both **(a)** a daily-driver
human search interface for the localmail archive and **(b)** a test harness for
the search subsystem's behavior. The defining requirement: search across **all
accounts and all folders by default**, with the ability to narrow scope when
useful. The visual language mimics Gmail (3-pane layout) for instant familiarity,
but the app is honest about being a **read-only archive browser** — it does not
send, reply, delete, move, or flag.

Eventually localmail will ingest decades of email from servers that are no
longer reachable. The GUI must work just as well over those imported archives
as over live IMAP-synced accounts.

## Constraints

These shaped every decision below.

- **Read-only with respect to IMAP.** The existing localmail invariant
  (`never delete, modify, or send`) is preserved. The GUI exposes no UI for
  destructive or upstream-write actions in v1, and the architecture leaves
  the door open for a future SMTP path via capability flags.
- **Architecture must not foreclose a future "proper email client".** This
  means a real API library, not ad-hoc SQL in the client, and a server-side
  capability model that the GUI honors.
- **Network-reachable from day one.** The user runs the GUI on a laptop and
  the data on a home server; both must be supported, single-host bundle
  must also be supported.
- **Zero Python in the client distribution.** The Tauri binary ships with no
  Python runtime, no model files, no DB driver. All compute happens on the
  server.
- **Privacy.** No telemetry, no SaaS calls. Same Golden Rules as the rest
  of the project apply.
- **Multi-user from day one (single bootstrap user OK).** Username/password
  auth on the API. A per-user account ACL ships as v1.x — the API shape
  must accommodate it without breaking changes.
- **Migration numbering.** Phase 2 hybrid search owns migrations through
  `0013` (in the `phase2-hybrid-search` worktree). GUI v1 schema additions
  start at `0014`.

## Architecture overview

Three processes, two hosts (or one host in the single-machine case):

```
┌─────────────────────────────────────────────────────┐
│ SERVER HOST                                         │
│                                                     │
│  ┌────────────────────┐   ┌──────────────────────┐  │
│  │ localmail run      │   │ localmail serve      │  │
│  │ (sync daemon)      │   │ (HTTP API server)    │  │
│  │ - IMAP IDLE/poll   │   │ - FastAPI + TLS      │  │
│  │ - parses, writes   │   │ - argon2 auth        │  │
│  │ - embed_worker     │   │ - imports api lib    │  │
│  └─────────┬──────────┘   └──────────┬───────────┘  │
│            │                         │              │
│            └──────► PostgreSQL ◄─────┘              │
│                     (shared)                        │
└─────────────────────────────────────────────────────┘
                         ▲
                         │ HTTPS + bearer token
                         │
┌────────────────────────┴─────────────────────────────┐
│ CLIENT HOST                                          │
│  ┌────────────────────────────────────────────────┐  │
│  │ localmail-gui  (Tauri 2 + Svelte + Rust core)  │  │
│  │  - Window, menus, IPC, HTTPS client            │  │
│  │  - Stores: server URL, username, token (OS     │  │
│  │    keyring), TLS cert pin (TOFU)               │  │
│  │  - No Python, no DB, no embedding model        │  │
│  └────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

### Key invariants

- **`localmail.api`** is the canonical Python library: search, get message,
  get attachment metadata, list accounts/folders/capabilities, auth. Pure
  library — no transport. The single source of truth for "how things query
  localmail".
- **`localmail serve`** is a thin FastAPI wrapper around `localmail.api`.
  Its only job is HTTP routing, auth middleware, TLS, request validation,
  JSON serialization. No business logic.
- **MCP server** (planned Phase 3 of search) is a *different* thin wrapper
  around the same `localmail.api`. Runs as its own process, imports the
  library directly — no HTTP hop. Exposes a deliberate subset of operations.
- **Tauri client** is Rust front-to-back. Ships no Python. Talks HTTPS+JSON
  to one configurable server URL. Stores credentials in the OS keyring
  (macOS Keychain / Windows Credential Manager / Linux Secret Service).
- **Daemon and serve never call each other.** They communicate via Postgres
  only. Either can be restarted, stopped, or absent without breaking the
  other.

### Deployment topologies (all supported by the same code)

1. **Single-host dev**: daemon + serve + GUI on one laptop. Serve binds
   `127.0.0.1`, plain HTTP allowed via explicit `--no-tls`, token written
   to a 0600 file under `$XDG_RUNTIME_DIR`.
2. **Always-on archive server**: daemon + serve on home server, GUI on
   laptop. Serve binds `0.0.0.0`, self-signed TLS by default, GUI does
   TOFU pinning on first connect.
3. **Archive-only host with no live sync**: only `localmail serve` runs
   (no `localmail run`). For imported archives from dead servers — no IMAP
   credentials needed, GUI still works.

## Stack decisions (locked)

| Concern | Choice | Notes |
|---|---|---|
| Server transport | FastAPI + uvicorn | Async framework; sync handlers run in threadpool (fine for current sync `psycopg`/`localmail.search` code). |
| Auth | Argon2id (`argon2-cffi`) + opaque bearer tokens, server stores SHA-256 | No JWT; server-side revocation matters more than statelessness at this scale. |
| TLS | Self-signed by default; client TOFU pinning | `--tls-cert`/`--tls-key` for user-provided certs; `--no-tls` only valid with `--bind 127.0.0.1`. |
| HTML sanitizer | `nh3` (Rust `ammonia` via PyO3) | Allowlist tags/attrs; external images blocked by default; `cid:` rewritten to `/v1/attachments/{sha256}`. CSS property allowlist enforced via `filter_style_properties`; dangerous tags removed with their content via `clean_content_tags`. |
| Client shell | Tauri 2 | Rust binary + webview; small footprint, native menus, cross-platform bundling. |
| Client UI | Svelte 5 + TypeScript | Small bundle, layout-friendly; revisitable during implementation if the user has a strong alternate preference. |
| Client HTTP | `reqwest` (Rust) with `rustls` | Rust-side; Svelte calls it via Tauri `invoke()` so JS never sees the bearer token. |
| Client storage | OS keyring (via `keyring-rs`) | Server URL, username, token, TLS cert pin. |
| Distribution | Tauri bundler → `.dmg` / `.msi` / `.AppImage` | Unsigned in v1; signing is a cost decision for later. |
| Threading model (v1) | **None** — flat results | Schema already has `messages.in_reply_to`; `references` parser-only. Threading is purely additive when added. |

## HTTP API surface

### Conventions

- **Base path**: `/v1/...`. Path-versioned; `/v2/` lives alongside `/v1/`
  when a hard break is ever needed.
- **Auth**: `Authorization: Bearer <token>`. Issued by `/v1/auth/login`;
  opaque 32-byte random, base64url-encoded; only returned once at login.
  Server stores `SHA-256(token)` in `api_tokens.token_sha256`.
- **Errors**: RFC 7807 `application/problem+json`.
- **Pagination**: cursor-based (`?cursor=…&limit=…`). Response includes
  `next_cursor` (`null` when done) and `total_estimate` where cheap to
  compute, omitted otherwise.
- **Time**: ISO 8601 UTC.
- **IDs**: server-side integer PKs serialized as strings in JSON.
- **CORS**: explicitly absent. The API is consumed by the Tauri client,
  not browsers. Browser clients fail loud.

### Endpoints

#### Server / version

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/v1/version` | none | `{api_major, api_minor, server_version}` — handshake. |
| `GET` | `/v1/health` | none | Liveness; does not touch DB. |
| `GET` | `/v1/capabilities` | required | `{search, attachments, attachment_text, threading, send}` — booleans. Lets the GUI hide UI for features the server doesn't support. |

#### Auth

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/auth/login` | `{username, password}` → `{token, expires_at}`. Rate-limited per username: 5 failures → 60s lockout (in-process sliding window). |
| `POST` | `/v1/auth/logout` | Revoke current token. |
| `POST` | `/v1/auth/refresh` | Auth required. Returns `{token, expires_at}` — issues a new token with a fresh 30-day window and revokes the presenting token. Used for silent renewal by the client. |
| `GET` | `/v1/auth/whoami` | Token introspection (username, expiry, granted accounts in v1.x). |

**Silent renewal**: the client calls `/v1/auth/refresh` automatically when
the current token's remaining lifetime drops below 7 days, on the next
authenticated request. New token is written to the OS keyring; the old
token is revoked server-side as part of the same call. The user is never
prompted unless `/v1/auth/refresh` itself fails (e.g., token was revoked
out-of-band, server unreachable past the original expiry), at which point
the standard login screen appears with the current view preserved.

#### Accounts & folders

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/accounts` | Array of `{id, name, address, capabilities: {can_sync, is_archive_only, is_shared}, last_sync_at, message_count}`. |
| `GET` | `/v1/accounts/{id}/folders` | Array of `{id, name, full_path, flags, message_count, last_uid}`. |
| `GET` | `/v1/folders/{id}/messages` | List messages newest-first, cursor-paginated, `MessageSummary` shape. |

#### Search

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/search` | Body: `{query, filters, limit, cursor}`. Returns `{results: [SearchResult], next_cursor, total_estimate, took_ms}`. |

Filter shape:

```json
{
  "account_ids": ["1", "3"],
  "folder_ids": ["5"],
  "date_from": "2024-01-01T00:00:00Z",
  "date_to": "2024-12-31T23:59:59Z",
  "has_attachment": true,
  "lang": "en",
  "from": "anna@",
  "to": "horst@",
  "subject": "school"
}
```

All filter fields are optional; omitted = no constraint. Empty arrays =
match nothing (explicit empty); missing key = match anything. The query
string itself supports the existing `query.py` DSL (`from:`, `to:`,
`subject:`); DSL hits are merged with structured filter fields (union of
constraints).

`SearchResult`:

```json
{
  "message_id": "1234",
  "account": {"id": "1", "name": "gmail.com"},
  "folder": {"id": "5", "full_path": "INBOX"},
  "subject": "Re: kid's school excursion",
  "from": {"name": "Anna H.", "address": "anna@…"},
  "to": [{"name": null, "address": "horst@gmail.com"}],
  "date": "2026-03-03T08:14:00Z",
  "snippet_html": "…bus leaves at <mark>7:30</mark> on Tuesday…",
  "has_attachments": true,
  "score": 0.84,
  "matched_arms": ["bm25_messages", "vector_chunks"]
}
```

`matched_arms` powers the opt-in **search debug** UI in the client.

#### Messages

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/messages/{id}` | Full message: subject, from/to/cc/bcc, date, headers (compact subset), `body_text`, `body_html` (sanitized), attachments array, account/folder breadcrumb, label list. Supports `?headers=full` to include the complete RFC822 header set (used lazily by the client's header unfold widget). |
| `GET` | `/v1/messages/{id}/raw` | Raw RFC822 bytes, `Content-Type: message/rfc822`. For "view source" + future export. |

HTML sanitization is server-side. Allowlist of tags + attrs; strip all
`<script>`, all `on*` handlers, scope `<style>`; `<img src="cid:...">`
rewritten to `/v1/attachments/{sha256}`; **all external `src` blocked
by default** (no tracking pixels, no remote image load). The client
shows a "Load images for this message" affordance per-message.

Response headers include:

```
Content-Security-Policy: default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'
```

so a sanitizer bypass cannot load remote resources or run scripts.

#### Attachments

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/attachments/{sha256}` | Stream blob bytes; `Content-Type` and `Content-Disposition` from the per-message `attachments` JSONB. Supports Range requests. |
| `GET` | `/v1/attachments/{sha256}/text` | Returns extracted text if `attachment_text` (migration 0011) has it. 404 otherwise. |

Attachments are keyed by SHA-256 globally (content-addressable). Per-message
filename lives in `MessageDetail.attachments`.

#### Changes (polling)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/changes?since=<cursor>` | `{new_messages: [MessageSummary], next_cursor}`. Client polls e.g. every 30s on the active folder/search. v1 returns new messages only (no deletions/flag changes — localmail is read-only w.r.t. IMAP). |

### Deliberately deferred

| Feature | Why deferred |
|---|---|
| `POST /v1/messages` (send) | No SMTP path yet. `capabilities.send` flag exists so the GUI can render a disabled compose button when capability lands. |
| Threading endpoints | Flat results in v1. `/v1/threads/{root_id}` is non-breaking when added. |
| User CRUD API | v1 ships a CLI bootstrap path (`localmail add-api-user`). Multi-user admin UI = v1.x. |
| Per-user account ACL | v1 = all users see all accounts. v1.x adds a `user_accounts` join + filter in search/list. API shape unchanged. |
| SSE / WebSocket push | Polling is sufficient. |
| Reindex / migration triggers | Operational; CLI only. |

## Schema additions

### Migration 0014 — API users and tokens

```sql
CREATE TABLE api_users (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,        -- argon2id
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at     TIMESTAMPTZ
);

CREATE TABLE api_tokens (
    token_sha256    BYTEA PRIMARY KEY,    -- SHA-256 of the bearer, not the bearer itself
    user_id         BIGINT NOT NULL REFERENCES api_users(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    last_used_at    TIMESTAMPTZ
);

CREATE INDEX api_tokens_user_id_idx ON api_tokens(user_id);
CREATE INDEX api_tokens_expires_at_idx ON api_tokens(expires_at);
```

### Notes on existing schema

- `messages.in_reply_to` already exists (since `0001_init.sql`). No change.
- `messages.references` is parsed but not stored; adding it is a trivial
  later migration when threading lands. Out of scope for GUI v1.
- `accounts` table: capabilities (`is_archive_only`, etc.) are derived at
  query time from the presence/absence of credentials, last sync time, and
  config — no new columns needed for v1. v1.x can promote them to columns
  if derivation becomes expensive.

## Code organization

```
src/localmail/
  api/                # NEW — canonical API library, transport-free
    __init__.py
    auth.py           # argon2, token issue/verify, sessions
    search.py         # wraps localmail.search.Searcher
    messages.py       # get_message, get_raw_rfc822, header projection
    accounts.py       # list accounts/folders, derive capabilities
    attachments.py    # metadata, byte streaming
    sanitize.py       # nh3-based HTML sanitizer
    errors.py         # typed exceptions → HTTP problem mapping
  serve/              # NEW — FastAPI HTTP wrapper
    __init__.py
    app.py            # FastAPI app factory
    routes/
      auth.py
      accounts.py
      folders.py
      messages.py
      attachments.py
      search.py
      changes.py
      version.py
    middleware.py     # auth, version, request ID, structured logging
    tls.py            # cert load / self-signed generate
  cli.py              # extended: serve, add-api-user, remove-api-user,
                      #           list-api-users, rotate-tls
migrations/
  0014_api_users.sql
```

Tauri client lives in a top-level `gui/` directory in this repository.
Same-repo keeps API and client changes atomic during v1; can be split
into its own repo later if release cadences diverge.

```
gui/                  # NEW — Tauri 2 + Svelte client
  src-tauri/          # Rust core: HTTP, keyring, TOFU, file dialogs
  src/                # Svelte UI: routes, components, stores
  package.json
  tauri.conf.json
  Cargo.toml
```

## Screen inventory & UX

### Layout (locked: Classic Gmail 3-pane)

- **Left rail (~220px)**: account/folder tree. Roots are accounts; children
  are folders. A pinned "All Mail" entry at the top represents the default
  cross-account scope. Archive-only accounts are visually marked
  (e.g., 📦 icon, muted color).
- **Middle column (~340px, resizable)**: result list. Each row: sender,
  subject, snippet, date, account/folder breadcrumb. Density configurable
  (comfortable/compact).
- **Right pane (flex)**: reading pane for the selected message.

### Screens

1. **First-run / connect** — Server URL, username, password. On TOFU,
   the cert SHA-256 is shown for confirmation.
2. **Login** — Same fields minus TOFU. Shown on token expiry/logout.
3. **Main view** — Layout A as above. Default state: "All Mail" selected,
   no search query, last 100 messages across all accounts shown.
4. **Reading pane** — Compact header block (From / To / Date / Account /
   Folder) with a small unfold widget to reveal full RFC822 headers
   (lazy-fetched via `?headers=full`). Body toggle: `HTML · Plain · Raw`.
   HTML default, external images blocked with per-message "Load images"
   affordance. Attachments strip at the bottom: filename / type / size /
   download. PDF and image attachments preview in a modal; everything else
   triggers OS save dialog.
5. **Attachment preview** — Modal with PDF.js or `<img>` for inline preview;
   non-previewable types skip the modal and go straight to save.
6. **Settings** — Server (URL, username, change password, log out, re-trust
   cert with pin SHA-256 displayed), Display (density, date format, HTML
   image policy), Search (debug toggle, page size, default language filter),
   About (API version, server version, client version, build hash, view logs).
7. **Empty / loading / error states** — Skeleton list during first paint,
   "no matches" with active-filter chips for one-click clear, "server
   unreachable" full-pane modal with retry, "token expired" inline re-auth
   preserving current view, "version mismatch" hard modal with "[Quit]",
   "search still running" spinner caption after 5s.
8. **Debug pane** — Opt-in via Settings. Result rows show per-arm scores
   and matched fields; reading pane shows extracted chunks with the
   matching chunk highlighted. Powered entirely by existing API data
   (`SearchResult.matched_arms`, `SearchResult.score`).

### Search interaction

- **Filter UI**: a "🔧 Filters" button next to the search bar opens a
  popover (Date, From, To, Has-attachment, Language). Active filters
  render as removable chips beneath the search bar; absent when no
  filters are active. Account and folder narrowing is via the tree
  (no chip needed).
- **DSL parity**: `from:anna has:attachment after:2024` in the search box
  is equivalent to setting filters via the popover. DSL and popover are
  independent expression paths; the server merges them.
- **Submission**: Enter or button click. No search-as-you-type in v1.
- **Cross-account default**: When "All Mail" is selected (default), filters
  scope `account_ids` to "all". Selecting an account or folder in the tree
  narrows the query to that scope, preserving any other active filters.

## Security posture

- TLS by default on any network-reachable bind. `--no-tls` only valid with
  `--bind 127.0.0.1`.
- Argon2id for passwords (`argon2-cffi` library defaults).
- Tokens are opaque random 32-byte, base64url-encoded; server stores
  SHA-256. Token expiry default 30 days, configurable.
- Login rate limited per username (5 failures → 60s lockout, sliding
  window, in-process — single host).
- TOFU cert pin stored client-side in OS keyring. Mismatch on reconnect
  prompts user with old/new SHA-256 side-by-side and "[Trust new] /
  [Cancel]".
- HTML bodies served with strict CSP (`default-src 'none'; img-src 'self'
  data:; style-src 'unsafe-inline'`) so a sanitizer bypass cannot load
  remote resources or run scripts.
- No CORS headers — API is for the Tauri client, not browsers.
- The bearer token never crosses the JS/Rust boundary in the client. Rust
  side holds it; Svelte calls `invoke('http_get', ...)` and Rust attaches
  the header.

## Logging & observability

- **Server**: structured JSON logs (stdlib `logging` + JSON formatter).
  One line per request with `request_id`, `user`, `path`, `status`,
  `duration_ms`, `query_hash` (hash, not plaintext — queries can contain
  sensitive substrings).
- **Client**: Rust `tracing` to a rolling log file in the platform's
  log directory (`~/Library/Logs/localmail-gui/` on macOS, equivalents
  elsewhere). 5MB cap.
- No external telemetry. No metrics push. Local logs only.
- "View logs" link in Settings opens the log directory in the OS file
  manager.

## CLI additions

```bash
uv run localmail serve [--bind 127.0.0.1] [--port 8443] \
                       [--tls-cert PATH] [--tls-key PATH] [--no-tls]
uv run localmail add-api-user USERNAME      # prompts for password
uv run localmail remove-api-user USERNAME
uv run localmail list-api-users
uv run localmail rotate-tls                 # regenerate self-signed cert
```

`localmail serve` is a foreground process. Run under systemd/launchd/tmux
like `localmail run`. No new daemonization code, no PID files.

## Testing posture

- **`localmail.api`**: pytest with existing conventions (`db_conn` fixture,
  `memory_keyring` autouse, `LOCALMAIL_TEST_DSN` against `localmail_test`).
  New fixtures: `api_user`, `api_token`, `api_client` (FastAPI `TestClient`).
- **`localmail.serve`**: every endpoint gets happy-path + auth-failure +
  malformed-request tests. TLS path tested with a tmpdir-generated cert
  pair. `/v1/version` and `/v1/health` exercised without auth.
- **Schema migration 0014**: smoke test that tables exist, constraints hold,
  argon2 round-trip works.
- **Client**: Rust unit tests for the HTTP client module (URL building,
  token attach, TOFU verification logic). Svelte component tests via
  `vitest`. End-to-end against a real `localmail serve` + ephemeral
  Postgres is deferred past v1.

## Performance assumptions

- Search p95 < 1.5s on a 100k-message archive (validated against the
  existing hybrid-search numbers during implementation).
- Single message fetch p95 < 200ms for typical sizes.
- Attachment streaming uses chunked transfer; no full-buffer in memory.
- HTTP/1.1 sufficient; no HTTP/2 requirement.
- Polling interval 30s by default, configurable.

## Future doors explicitly left open

- **SMTP send** — `POST /v1/messages` + `capabilities.send = true`. GUI's
  compose button driven by the capability flag, hidden in v1.
- **Threading** — `GET /v1/threads/{root_id}` + `MessageSummary.thread_id`
  + migration adding `messages.references` and `messages.thread_id`. Purely
  additive.
- **Per-user account ACL** — `user_accounts` join table; `/v1/accounts`
  filters its response. No API shape change.
- **MCP server** — imports `localmail.api` directly, exposes
  `search.run`, `messages.get`, `accounts.list` (probably; final scope
  decided in Phase 3 of search).
- **Real-time push (SSE)** — `GET /v1/events` alongside or replacing
  `/v1/changes`. Polling client keeps working.
- **Multi-archive federation** — client config grows from one server to
  a list; same protocol.

## Open questions for the implementation phase

These do not need answers to start; they need answers somewhere in the
implementation plan.

1. **`accounts.is_shared` semantics.** v1 ships with this flag always
   `false`. Define what it means before v1.x adds the ACL feature so the
   flag isn't a leaky abstraction.
2. **Self-signed cert lifetime.** 1 year, 10 years, indefinite? `localmail
   rotate-tls` exists either way. Recommendation: 10 years (it's
   self-signed, the lifetime is administrative not security).
3. **First-run UX when no `api_users` exist.** Should `localmail serve`
   refuse to start, or accept connections and return a "no users
   configured" error from `/v1/auth/login`? Recommendation: start
   normally, return a clear error from login attempts; the CLI message
   on `serve` startup tells the operator to run `add-api-user`.
4. **Body HTML vs text default.** Spec says HTML by default. Some users
   may prefer plain-text by default for security/aesthetic reasons. A
   single Settings toggle covers it; mention in v1 release notes.
