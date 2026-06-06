# localmail

<p align="center">
  <img src="assets/banner medium.png" alt="localmail" width="480">
</p>

Mirrors one or more IMAP accounts (password or Gmail OAuth2) into a local
PostgreSQL database. The archive is **read-only with respect to upstream**:
localmail never deletes, modifies, or sends mail. Downstream agents read from
Postgres and the attachment tree without touching IMAP.

## Layout

- Email rows + headers + raw RFC822 + extracted plaintext/HTML live in Postgres.
- Attachments are stored content-addressably at
  `<attachments.root>/blobs/<aa>/<bb>/<full-sha256-hex>` — identical bytes are
  written exactly once across the whole archive regardless of how many emails
  or accounts reference them. Each message's `attachments` JSONB column
  records `[{"filename": "<original-name-from-this-email>", "sha256": "<hex>"}, …]`,
  preserving the *per-email* filename so files can be restored with the names
  they had when received.
- Secrets (IMAP passwords, OAuth refresh tokens) live in the OS keyring —
  macOS Keychain on darwin, Secret Service (gnome-keyring / KWallet) on Linux.
- Per-account topology (host, email, auth method, folder allow/deny) lives in
  the `accounts` database table. A single TOML file seeds it on `init-db`; the
  DB is authoritative thereafter.

## Quickstart

```bash
# 1. Install dependencies
uv sync

# 2. Configure
mkdir -p ~/.config/localmail
cp config.example.toml ~/.config/localmail/config.toml
# edit ~/.config/localmail/config.toml: set DSN, attachments root, add [[accounts]]

# 3. Initialise schema
uv run localmail init-db

# 4. Store credentials
uv run localmail add-account work-fastmail           # password account
uv run localmail oauth-login horst-gmail             # Gmail OAuth (browser opens)

# 5a. One-shot sync (good for cron, smoke testing, initial backfill)
uv run localmail sync
uv run localmail sync --account horst-gmail
uv run localmail sync --account horst-gmail --limit-per-folder 10   # smoke test

# 5b. Or run the daemon (IDLE on INBOX + periodic poll on other folders)
uv run localmail run        # foreground; supervise via systemd / launchd
```

## CLI

### Sync & accounts

> **The database is canonical for accounts.** `config.toml` `[[accounts]]`
> blocks are a *seed*: `localmail init-db` merges them into the `accounts`
> table, after which the DB is authoritative. The daemon, the one-shot `sync`,
> and every account command read and write the DB — not the TOML. A TOML block
> still serves as the seed source for `add-account` / `oauth-login` when the
> named row does not exist yet, but editing TOML after a row exists has no
> runtime effect (a drift warning is logged at `init-db`).

| Command | Purpose |
| --- | --- |
| `localmail init-db` | Apply pending schema migrations, then seed `[[accounts]]` from `config.toml` into the database. Idempotent; the DB is authoritative, so existing rows are never overwritten (a drifted TOML value logs a warning and is ignored). |
| `localmail list-accounts` | Show accounts in the database and whether a secret is stored. |
| `localmail add-account NAME` | Prompt for an IMAP password and store it in the keyring. Resolves `NAME` against the DB; if absent but declared in `config.toml`, the DB row is created from that block first. |
| `localmail oauth-login NAME` | Run the Gmail OAuth desktop consent flow. Stores the refresh token in the keyring. Resolves `NAME` against the DB (seeding from `config.toml` if absent). |
| `localmail remove-account NAME [--delete-row] [--force]` | Clear stored secrets for an account. `--delete-row` also removes the DB account row (`--force` cascades when messages reference it). |
| `localmail enable-account NAME` / `localmail disable-account NAME` | Resume or pause syncing for an account by flipping `sync_enabled` in the DB. A paused account spawns no daemon threads; a one-shot `localmail sync --account NAME` still runs it. Archive accounts are rejected; re-running on an account already in the target state is a no-op. |
| `localmail sync [--account NAME] [--limit-per-folder K] [--no-ssl]` | One-shot incremental sync over the syncable database accounts (live + `sync_enabled`). `--account NAME` syncs one account even if it is paused (`sync_enabled = false`); archive accounts are rejected. |
| `localmail run [--log-level …] [--no-ssl]` | Foreground daemon: per-account IDLE thread on INBOX + periodic poll thread for other folders. **Hot-reloads accounts** — add/remove/pause/resume an account or rotate its credentials and the running daemon converges within `[daemon] reload_seconds` (default 30 s), no restart needed. SIGTERM/SIGINT shut down cleanly. If Postgres is briefly unreachable at launch, startup retries with bounded exponential backoff (`[daemon] startup_backoff_initial_s`→`startup_backoff_max_s`, default 1→60 s) rather than crashing; the daemon's fresh (non-pool) connects — startup account read, reconcile, heartbeat clear — are bounded on every phase so no network fault stalls startup or hot-reload for the OS TCP default: the TCP connect by `[daemon] db_connect_timeout_s` (default 10 s), server-side query execution by `[daemon] db_statement_timeout_s` (default 30 s, `0` disables — catches a slow/stuck query), and a post-connect black-hole (packets dropped *after* connect) by `[daemon] db_tcp_user_timeout_ms` (default 30000 ms, `0` = OS default; libpq `tcp_user_timeout`, Linux-effective, ignored on platforms without `TCP_USER_TIMEOUT`). The daemon records per-thread liveness heartbeats in the `daemon_heartbeats` table — covering each account's IDLE + poll threads plus the embed/extract/reconcile process workers — and a heartbeat is considered stale after `[daemon] heartbeat_stale_seconds` (default 120 s); the admin daemon-status endpoint (2B.4) exposes this liveness state. The running daemon also drains a `daemon_commands` queue at the top of each reconcile tick — `reload-now` (converge immediately instead of waiting out `reload_seconds`), `restart-account` (tear down + respawn one account's threads, e.g. for a wedged connection), and `drain-stop` (graceful shutdown) — and `LISTEN`s for an enqueue `NOTIFY` so a queued command wakes the loop at once rather than on the next poll (disable the listener with `[daemon] command_listen_enabled = false`; its poll interval is `[daemon] command_listen_poll_seconds`, default 5 s). Enqueue these commands via the `localmail daemon` CLI subgroup or the admin HTTP routes (2B.4). |
| `localmail list-failed [--account NAME] [--limit K]` | Show messages that sync skipped due to errors. |
| `localmail retry-failed [--account NAME]` | Re-attempt every failed message. Successful retries move from `failed_messages` to `messages`. |

### Daemon control (2B.4 / 2B.5)

Two control planes. **Plane A** (DB-mediated) works whether the daemon is
supervised by `localmail serve` or by an init system; **Plane B** (process
lifecycle) requires `localmail serve` to be running with
`[serve] supervise_daemon = true` (the default), which owns `localmail run` as
a child and binds a Unix control socket at
`${runtime_dir}/localmail-supervisor.sock` (mode 0600).

| Command | Purpose |
| --- | --- |
| `localmail daemon status` | Show daemon process state (from the supervisor socket when supervised, `external` otherwise) plus per-thread liveness from `daemon_heartbeats`. Heartbeats always print; an unreachable socket is reported, not an error. |
| `localmail daemon reload` | **Plane A.** Enqueue `reload-now` so the running daemon re-reads its account set immediately instead of waiting out `reload_seconds`. |
| `localmail daemon restart-account NAME` | **Plane A.** Enqueue `restart-account` to tear down and respawn one account's IDLE + poll threads (e.g. a wedged connection). |
| `localmail daemon start` / `stop` / `restart` [`--no-wait`] | **Plane B.** Drive the supervised child over the control socket. The supervisor runs the lifecycle op on its own thread and returns at once, so the command **polls status until the daemon settles** (running / stopped) — `--no-wait` skips the poll and prints the transitional state. Exits non-zero with a clear note when the daemon is supervised externally (`supervise_daemon = false`) or when `localmail serve` is not running. |

The admin UI (`localmail serve`) also exposes a **daemon-control panel** at
`/admin/daemon`: a status table (per-thread state, current folder, heartbeat
age — red when stale — and last error), the same start / stop / restart /
reload / per-account restart-sync controls, and a recent-log tail, refreshed
live via an HTMX poll. The lifecycle buttons are disabled with a note when the
daemon is supervised externally. The HTTP lifecycle routes
(`POST /v1/admin/daemon/{start,stop,restart}`) return **202 Accepted** with the
transitional status — the panel and CLI poll `GET /v1/admin/daemon` to observe
the daemon settling. A rejected control action (a busy-guard **409** while
another lifecycle op is in flight, or a stale-token **400**) surfaces a brief
toast on the panel rather than leaving the button looking inert.

The **account management panel** at `/admin/accounts` provides server-rendered
HTMX screens to list, create, edit, and delete IMAP accounts; store passwords
in the keyring; run test-connection (lists live IMAP folders, including Gmail
OAuth2 accounts, and reports a genuine connect failure — wrong host/port/password,
DNS, TLS — as a friendly inline error rather than a server error); and
enable/disable per-account sync. The Gmail "Connect"
button starts the OAuth2 consent flow; on completion the browser lands on the
edit page (`/admin/accounts/{id}?oauth=success`). All mutating actions carry
method-bound CSRF tokens (`X-CSRF-Token` header).

The **user management panel** at `/admin/users` provides server-rendered HTMX
screens to create and delete API users, grant or revoke per-account access (a
checklist over every account on the user's edit screen), toggle admin rights,
reset passwords, enable/disable accounts, and revoke a user's outstanding
sessions. The UI refuses any action that would remove the last remaining admin
or lock out your own account, and renders such controls disabled. The same
operations are exposed as a JSON API under `/v1/admin/users`.

### Importing archive mail

localmail can import existing mbox files or maildir directories into an
`archive` account. The import is idempotent — already-imported messages are
skipped via the existing per-account Message-Id / raw-SHA256 dedup, so
re-running is safe.

1. **Create an archive account** at `/admin/accounts` (auth method: `archive`,
   no IMAP host required), or via the CLI:
   `localmail add-account NAME` (with `auth_method = "archive"` in `config.toml`).

2. **Allow the source directory** in `config.toml`:

   ```toml
   [imports]
   roots = ["/path/to/archives"]   # empty = imports disabled
   ```

3. **Start an import** from the `/admin/imports` panel (select the archive
   account, the source kind `mbox` or `maildir`, and the path), or run it
   directly from the CLI:

   ```bash
   uv run localmail import /path/to/archives/backup.mbox --account myarchive --kind mbox
   uv run localmail import /path/to/archives/maildir/    --account myarchive --kind maildir
   ```

   The path must resolve under one of the configured `roots`; imports are
   rejected otherwise. The received date from each source message (mbox `From_`
   envelope line or maildir file mtime) is stored as `messages.internal_date`.
   Progress and status are visible in the `/admin/imports` panel; a job idle
   longer than `[imports].stale_seconds` (default 60) is shown in red. Progress
   counters and the Cancel button update at least every
   `[imports].checkpoint_seconds` (default 2) — and after the first message — so
   even a small or slow import (a few large attachments) stays responsive. A CLI
   import in progress is unaffected by restarting the `serve` process: startup
   reconciliation only clears import jobs whose owning process is actually gone,
   so the live CLI job keeps running and still blocks a second concurrent import.

### Search backfill & status

| Command | Purpose |
| --- | --- |
| `localmail embed-backfill` | Drain the message-chunk embedding queue in the foreground; exit when empty. Also drains the language-detection queue after embeddings finish. |
| `localmail extract-backfill [--no-progress]` | Drain the attachment-extraction queue (Phase 2): extract text from PDFs, DOCX, etc. |
| `localmail lang-backfill [--no-progress]` | Populate `messages.body_lang` for every message with NULL body_lang. Required once after first install so the `lang:` search token returns rows. |
| `localmail search "QUERY" [--format text\|json]` | Hybrid lexical + vector search over the local archive (see [Search](#search) below). |
| `localmail search-status [--format text\|json]` | Report chunk/extraction backlog, language-detection progress, and failure counts. |
| `localmail list-failed-embeddings` | Show recent `failed_embeddings` rows. |
| `localmail retry-failed-embeddings` | Clear `failed_embeddings` so the embed worker re-picks them up. |
| `localmail list-failed-extractions` | Show recent `failed_extractions` rows. |
| `localmail retry-failed-extractions` | Clear `failed_extractions` + `transient_extractions` so the extract worker re-picks them up. |

### GUI server (HTTPS API)

| Command | Purpose |
| --- | --- |
| `localmail serve [--bind 127.0.0.1] [--port 8443] [--tls-cert PATH] [--tls-key PATH] [--no-tls]` | Run the HTTPS API server. TLS is mandatory unless `--bind 127.0.0.1 --no-tls`. |
| `localmail add-api-user USERNAME [--password TEXT \| --password-stdin]` | Create an API user (argon2id-hashed password). New users have **no account grants** — they see no mail until `grant-account` is run. |
| `localmail list-api-users [--with-grants]` | List configured API users. `--with-grants` shows each user's account grants. |
| `localmail remove-api-user USERNAME` | Delete an API user and all its tokens. |
| `localmail grant-account USERNAME ACCOUNT_NAME` | Grant `USERNAME` read access to `ACCOUNT_NAME` (per-user account ACL). Idempotent. |
| `localmail revoke-account USERNAME ACCOUNT_NAME` | Revoke `USERNAME`'s access to `ACCOUNT_NAME`. |
| `localmail rotate-tls --cert PATH --key PATH [--hostname H] [--force]` | Generate (or regenerate) a self-signed TLS cert + key. |
| `localmail grant-admin USERNAME` / `localmail revoke-admin USERNAME` | Toggle `api_users.is_admin` for the admin UI. Shell-only bootstrap. |
| `localmail revoke-admin-sessions USERNAME` | Invalidate all of `USERNAME`'s admin cookie sessions (admin privilege itself is untouched — use `revoke-admin` for that). |

> **Upgrading to migration 0016.** Per-user account ACL is now enforced
> at the API boundary: by default a freshly-created user can read **no**
> accounts and every `/v1/*` call returns empty lists / 404s. Run
> `localmail grant-account USERNAME ACCOUNT_NAME` once per (user, account)
> pair to restore the pre-0016 default-allow posture. Pre-existing users
> need explicit grants for any account they should keep reading. See
> [docs/superpowers/specs/2026-05-18-per-user-account-acl-design.md](docs/superpowers/specs/2026-05-18-per-user-account-acl-design.md)
> for the full design.

> **Upgrading on a populated archive?** Before running `localmail
> init-db` against a large pre-existing `messages` table, read
> [docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md)
> and run `localmail estimate-upgrade` first. Migration 0006 holds an
> `ACCESS EXCLUSIVE` lock for the duration of an `ADD COLUMN ...
> GENERATED STORED` table rewrite, which can take minutes to hours
> on a multi-million-row archive.

## Gmail OAuth2 setup

Gmail requires OAuth2 for IMAP since 2022. To configure an OAuth2 account:

1. **Create a Google Cloud project** (free):
   - Go to <https://console.cloud.google.com/projectcreate>.
   - Pick any project name; no billing required.

2. **Configure the OAuth consent screen**:
   - APIs & Services → OAuth consent screen.
   - User type: **External**.
   - Fill in app name, support email, developer email.
   - Scopes: leave blank in the form (the IMAP scope is requested at runtime).
   - **Test users (CRITICAL — skip this and you will get `Error 403:
     access_denied` at consent time):** in the new console UI this lives at
     OAuth consent screen → **Audience** → **Test users** → **+ Add users**.
     Add the literal Google account address you will sign in with — Google
     ignores dots in Gmail addresses for *routing* (`a.b@gmail.com` and
     `ab@gmail.com` are the same mailbox), but the test-users list expects
     the canonical form the account is registered under. Leave the project
     in "Testing" status indefinitely; promoting to production would require
     Google's verification audit because `https://mail.google.com/` is a
     restricted scope.

3. **Enable the Gmail API** (the IMAP scope lives under it):
   - APIs & Services → Library → Gmail API → Enable.

4. **Create OAuth client credentials**:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID.
   - Application type: **Desktop**.
   - After creation, click **Download JSON**. Save it as
     `~/.config/localmail/gmail_client_secret.json`
     (or anywhere; just point `[gmail_oauth] client_secrets_file` at it in
     config.toml).

5. **Configure the account in config.toml**:

   ```toml
   [gmail_oauth]
   client_secrets_file = "~/.config/localmail/gmail_client_secret.json"

   [[accounts]]
   name              = "horst-gmail"
   email             = "you@gmail.com"
   imap_host         = "imap.gmail.com"
   auth_method       = "oauth2"
   oauth_provider    = "gmail"
   # Flag-based denial (RFC 6154) — survives locale renames (e.g. Bin vs Trash)
   # and provider differences. Prefer this over folder_deny by name.
   folder_deny_flags = ["\\Trash", "\\Junk", "\\All"]
   ```

   `name` is the account's canonical key (keyring entry, database row, the
   `init-db` seed key), so it must be unique across all `[[accounts]]` blocks —
   a duplicate fails config load with an error naming the offending name.

6. **Run the consent flow once**:

   ```bash
   uv run localmail oauth-login horst-gmail
   ```

   This opens a browser, you grant access, and the refresh token is written
   to the keyring. Sync uses the refresh token to mint short-lived access
   tokens — no further interaction required unless you revoke access at
   <https://myaccount.google.com/permissions>.

### Why deny `\All`?

Gmail surfaces every message under both its INBOX/label folders *and* under
`[Gmail]/All Mail` (the `\All` special-use folder). localmail dedups by
Message-Id per account, so the same message in INBOX and All Mail produces
one `messages` row with two `message_labels` rows. That's fine, but
`All Mail` adds no new information. Excluding it roughly halves the upfront
sync time.

## Recovering from failed messages

Sync wraps each message in a Postgres SAVEPOINT. If a single message hits an
unexpected exception (a poison-pill encoding, an edge case the parser
chokes on, etc.) only that message is rolled back — the surrounding batch
keeps going. The raw RFC822 bytes + error details are stored in the
`failed_messages` table for later recovery:

```bash
uv run localmail list-failed                # show the queue
uv run localmail retry-failed               # re-attempt with current parser
uv run localmail retry-failed --account N   # one account only
```

Successful retries delete the `failed_messages` row and insert the message
into `messages` via the same code path live sync uses. Persistent failures
bump `retry_count` and `last_retry_at`.

Attachment extraction follows the same SAVEPOINT discipline but distinguishes
two error classes (so a docling model-download blip doesn't permanently mark
a perfectly fine PDF as failed):

- **Transient** — `TransientExtractorError`, `ConnectionError`, `TimeoutError`,
  or `MemoryError` anywhere in the cause chain. Rolled back to the per-blob
  SAVEPOINT, logged as a WARNING, **no** `failed_extractions` row written.
  The blob remains eligible for the next sweep with `retry_count` untouched.
  docling's third-party network failures (`requests` / `httpx` / `urllib3` /
  `huggingface_hub` / `aiohttp`, e.g. a model-fetch blip) aren't builtin
  `ConnectionError` subclasses, so `DoclingExtractor` opts them into
  `TransientExtractorError` at the wrapper — they retry instead of being
  recorded as poison-pills. So that a *permanently* failing third-party network
  error (e.g. a `huggingface_hub` 401/403 from a misconfigured token, or a 404
  for a removed model) can't loop the worker forever, transient failures are
  counted in a separate `transient_extractions` table — independent of
  `retry_count`. After `search.extract_worker_max_transient_retries` (default 5)
  **consecutive** transient failures the blob stops being re-attempted and one
  distinct *"giving up"* WARNING is logged; a successful extraction resets the
  counter (#153, resolved).
- **Poison-pill** — corrupt PDF, encrypted, parser raise, anything else.
  Recorded in `failed_extractions` with `retry_count += 1`, permanently
  skipped once `retry_count >= search.extract_worker_max_retries` (default 3).

```bash
uv run localmail list-failed-extractions      # show recorded poison-pills
uv run localmail retry-failed-extractions     # clear failed + stuck-transient state so they re-queue
```

## GUI server

`localmail serve` exposes the local archive over a small HTTPS API consumed
by the desktop client (and any other downstream tool that wants a network
boundary instead of direct DB access). Routes live under `/v1/`:
`auth`, `accounts`, `messages`, `attachments`, `changes`, `search`,
`capabilities`, `version`, `health`.

```bash
# 1. One-time: create a TLS cert (self-signed is fine for localhost / LAN).
uv run localmail rotate-tls \
  --cert ~/.config/localmail/tls.crt \
  --key  ~/.config/localmail/tls.key

# 2. Create an API user (argon2id-hashed password in the DB).
uv run localmail add-api-user alice

# 3. Run the server.
uv run localmail serve \
  --bind 127.0.0.1 --port 8443 \
  --tls-cert ~/.config/localmail/tls.crt \
  --tls-key  ~/.config/localmail/tls.key
```

`--bind 0.0.0.0` requires TLS (refused otherwise). `--no-tls` is only
honoured on `127.0.0.1` for local dev.

### Login rate-limit config

```toml
[auth]
# Login rate-limit thresholds (all Postgres-backed; survive
# uvicorn --workers N and serve restarts).
login_per_user_max = 5
login_per_user_window_s = 60
login_per_ip_max = 20
login_per_ip_window_s = 60
login_global_max = 30
login_global_window_s = 60
login_attempt_retention_s = 86400  # 24h
login_cleanup_interval_s = 300     # 5m
```

> The three login-rate-limit caps (global / per-IP / per-user) are
> Postgres-backed, so they survive `localmail serve` restarts and apply
> consistently across `uvicorn --workers N`. Behind a reverse proxy,
> configure `auth.trusted_proxies` (see below) so the per-IP cap reads
> the real client from `X-Forwarded-For` instead of the proxy's IP.

### Behind a reverse proxy

The login rate limiter has separate global, per-IP, and per-user caps.
Behind a reverse proxy, `request.client.host` is the proxy's address —
not the real client — so every login appears to come from the proxy
and the per-IP cap collapses into a copy of the global cap.

Configure `auth.trusted_proxies` (a list of CIDRs) to recover the real
client IP from `X-Forwarded-For`. The list governs both admission ("is
this socket peer a trusted proxy?") and peeling ("which XFF entries
are proxies vs the client?"). Right-to-left peel of XFF — identical
to nginx's `set_real_ip_from` / Caddy's `trusted_proxies` semantics.

```toml
[auth]
# Same-host nginx/Caddy/Traefik fronting localmail serve on 127.0.0.1:
trusted_proxies = ["127.0.0.0/8"]

# Reverse proxy on a separate host in a private LAN:
# trusted_proxies = ["10.0.0.0/8", "127.0.0.0/8"]

# For a CDN/edge proxy (Cloudflare, Fastly, etc.) fetch the operator's
# current published IP ranges (e.g. https://www.cloudflare.com/ips/);
# they change over time, don't hard-code stale CIDRs from this README.

# Hard cap on entries we walk before giving up. Defaults to 3
# (client → CDN → ALB → app). Bump if your chain is longer.
# trusted_proxies_max_hops = 3
```

Default is `[]` — unchanged behaviour; the socket peer is used. Bad
CIDR values fail loud at config load.

**Do not combine this with `uvicorn --forwarded-allow-ips`.** That flag
rewrites `request.client.host` to the XFF-derived value before the
FastAPI handler runs, which defeats the admission check and lets any
direct client spoof the per-IP cap.

**Make sure your proxy actually sets `X-Forwarded-For`.** When the
socket peer is trusted but no XFF header is present (or it's empty),
the resolver falls back to the proxy's own IP — every client behind
that misconfigured proxy then lands in a single per-IP rate-limit
bucket. This is a proxy-config bug, not a localmail bug, but the
symptom (legitimate users tripping the per-IP cap) looks the same.
nginx: `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`.
Caddy and Traefik set it by default.

### Browse & search pagination

`GET /v1/messages` returns one keyset-paginated page of messages in
`COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC` order
(the same ordering the search subsystem and `/v1/changes` use). The
response carries `next_cursor`; pass it back as `?cursor=…` to walk
the archive. The cursor is opaque (URL-safe base64) — do not parse it
client-side.

`/v1/messages` is the canonical **backfill / "load older"** endpoint;
`/v1/changes` is the live-tail subscription (capped at 200 rows per
call, with or without a `since` cursor). Clients use `/v1/messages`
for initial mail-list load and history scroll, and `/v1/changes` for
polling new arrivals.

`GET /v1/search` supports two cursor flavours, transparently:

- **Pool cursor** (`"<token>:<page>"`) — paged result from the
  hybrid retrieval pool. When the page advances past the cached pool
  and `can_grow_pool=true`, the route doubles `candidates_per_arm` up
  to `candidates_per_arm_max` (default 800), then flips
  `next_cursor` to `null`.
- **Keyset cursor** (`"K|<base64>"`) — used for `sort=date` +
  non-empty query, backed by a lexical FTS scan over
  `COALESCE(internal_date, date_sent)`. Unbounded scroll; no pool
  cap. Same recall as the lexical retrieval arm.

If a paged cursor's underlying pool was evicted from the in-memory
cache (TTL expiry, LRU eviction, or `serve` restart) the route
returns HTTP 409 with `type: /problems/search-cursor-expired`. The
GUI handles this transparently by re-issuing the original query
without a cursor and skipping past rows it already holds.

Wire `date` on every paginated response (`/v1/messages`, `/v1/search`,
`/v1/changes`) is `COALESCE(internal_date, date_sent)` — the same key
the SQL sorts by — so the displayed ordering always matches the field.
Legacy archives can backfill IMAP `INTERNALDATE` via
`localmail backfill-internal-date` once after upgrade.

## MCP server

`localmail serve` can also expose the archive to AI agents over the
[Model Context Protocol](https://modelcontextprotocol.io/). The MCP server is
mounted **inside the same `localmail serve` app** at `/mcp` over Streamable HTTP
(no new listener; same TLS rules as `serve`). It is read-only and gives an agent
the archive's read surface as MCP tools.

Enable it with the optional extra plus a config flag (both required; default
off):

```bash
uv sync --extra mcp           # pulls mcp>=1.13.0
```

```toml
[mcp]
enabled = true
```

If the extra is absent, `serve` still runs and logs an INFO line skipping the
mount. The endpoint is `https://<host>:<port>/mcp`.

Auth is an **opaque bearer token** reusing `api_tokens`: an agent obtains a token
via `POST /v1/auth/login` (refresh via `/v1/auth/refresh`) and passes it as
`Authorization: Bearer <token>`. Every tool is scoped to the user's per-account
grants (`localmail grant-account`), so a new user sees no mail until granted.

Five read-only tools:

- `search` — hybrid lexical + vector search; page forward with `next_cursor`.
- `get_message` — one message's headers, body, and attachment list.
- `get_attachment` — an attachment's extracted **text** or metadata (never raw
  bytes; download those from `GET /v1/attachments/{sha256}`).
- `list_messages` — keyset date-ordered browse, newest first.
- `list_accounts` — the accounts the agent may read.

See [docs/mcp-usage.md](docs/mcp-usage.md) for the full operator + agent guide
(setup flow, tool parameters, ACL scoping, and the config block).

## GUI client

A Tauri 2 + Svelte 5 desktop client lives in [gui/](gui/). It talks to a
running `localmail serve` instance over HTTPS and provides search,
message-reading, attachment download, and connection / account management.
See [gui/README.md](gui/README.md) for develop / build instructions.

```bash
cd gui
npm install
npm run tauri dev          # development window with hot reload
npm run tauri build        # produces a platform-specific release bundle
```

## Development

```bash
uv sync
uv run pytest                # full suite (~800 tests); skipped if no Postgres
uv run localmail --help
```

`tests/conftest.py` auto-skips DB-dependent tests if no Postgres is reachable
at `LOCALMAIL_TEST_DSN` (defaults to
`postgresql://localmail:local%40%40mail@localhost:5532/localmail_test` — a
separate database from the live archive, so tests can't clobber real data).

CI: `.github/workflows/python-ci.yml` runs the full pytest suite on every
push to `main` and every PR touching `src/`, `tests/`, `migrations/`,
`pyproject.toml`, `uv.lock`, or the workflow itself. The runner uses a
`pgvector/pgvector:pg18` service container so migration 0004's
`CREATE EXTENSION vector` clause works without extra setup. The Tauri/Svelte
GUI is covered by a separate `.github/workflows/gui-ci.yml`.

## Search

`localmail` ships a hybrid lexical (tsvector) + vector (pgvector) search
subsystem. Once initial backfill completes, you can search the local archive
from the CLI, from Python, or via the GUI / HTTPS API.

- **Phase 1** — message-body hybrid search (RRF fuse of three retrieval arms,
  followed by a cross-encoder rerank).
- **Phase 2** — attachment-text search: PDFs, DOCX, etc. are decoded by
  `docling` (install the `extraction` extra), chunked, embedded, and joined
  into the same fused result list.

### Setup

```bash
# Apply migrations (creates message_chunks, attachment_text tables, indexes).
uv run localmail init-db

# Backfill message-body embeddings. First run downloads ~250 MB of model
# weights to ~/.cache/fastembed/ (one-time). This also drains the
# `messages.body_lang` queue via `lingua-language-detector` once embedding
# finishes, so `lang:` filters work without a second command.
uv run localmail embed-backfill

# (Optional) Run only the body_lang pass — useful when chunks/embeddings
# are already up to date but body_lang is not (e.g. after upgrading from a
# pre-body_lang archive, or after raising `body_lang_min_confidence`).
uv run localmail lang-backfill

# (Optional, Phase 2) Backfill attachment text for an existing archive.
# Requires the docling optional dep: `uv sync --extra extraction`.
uv run localmail extract-backfill

# Progress at any time (includes body_lang_populated / body_lang_pending):
uv run localmail search-status
```

### Search from the CLI

```bash
uv run localmail search "Berlin conference"
uv run localmail search "invoice has:attachment after:2025-01-01 from:anna"
uv run localmail search "Heizung" --format json | jq
uv run localmail search "minutes lang:en before:2025-06-01"   # language + date filters
```

DSL operators: `from:`, `to:`, `subject:`, `label:`, `account:`, `folder:`,
`account_id:N`, `folder_id:N`, `after:YYYY-MM-DD`, `before:YYYY-MM-DD`,
`lang:XX` (ISO 639-1 code; matches the `messages.body_lang` column populated
per-message by `lingua-language-detector`), and `has:attachment`. Each operator
may appear multiple times where it makes sense (e.g. multiple `lang:`
accumulate; `lang:en lang:de` matches either). Bodies shorter than
`search.body_lang_min_text_chars` (default 20) and detections below
`search.body_lang_min_confidence` (default 0.65) stay NULL and are excluded
from `lang:` queries.

### Search from Python

```python
from localmail.search import create_searcher

searcher = create_searcher()
page = searcher.search("Berlin conference", page_size=20)
for r in page.results:
    print(r.rank, r.score, r.subject, r.snippet)

# Page 2:
page2 = searcher.continue_page(page.search_token, page=2)

# Needle-in-haystack — widen the candidate pool:
deeper = searcher.grow_pool(page.search_token, candidates_per_arm=200)
```

### Embedding model

The default model is **EmbeddingGemma-300M** (`google/embeddinggemma-300m`),
distributed under the [Gemma Terms of Use](https://ai.google.dev/gemma/terms).
The model weights are downloaded at runtime by fastembed; by using the
default you accept those terms.

For a strictly Apache-2.0 alternative:

```toml
[search]
embedding_model = "Snowflake/snowflake-arctic-embed-l-v2.0"
embedding_dim = 1024
```

Re-run `localmail embed-backfill` after switching models (the design
supports coexisting `embedding_v1` / `embedding_v2` columns for in-place
migration — see Phase 5).

### Tuning

All knobs live in `[search]` in `~/.config/localmail/config.toml`. The
defaults are calibrated for hundreds of thousands of messages on a
modern laptop. The most likely knobs to touch:

- `candidates_per_arm` (default 50) — increase for hard queries
- `candidates_per_arm_max` (default 800) — ceiling for transparent
  `grow_pool` growth on the `/v1/search` cursor path; once hit,
  `next_cursor` flips to null
- `rerank_pool_size` (default 100) — sized so the first `sort=rank` page
  fills the GUI's `limit=50` and one follow-up serves from cache before
  `grow_pool` fires
- `reranker_enabled` (default **false**) — the CPU-bound cross-encoder
  rerank pass overruns request timeouts when the cursor's `grow_pool`
  doubles the pool repeatedly (50 → 100 → 200 → 400 → 800). Flip to
  `true` on GPU hosts via `config.toml`
- `chunk_size_tokens` (default 512) — smaller for short messages
- `body_lang_enabled` (default true) — set false to skip language detection
- `body_lang_min_confidence` (default 0.65) — lower to label more messages
  (and accept more wrong labels); raise to be stricter
- `body_lang_low_accuracy` (default true) — ~100 MB resident; set false for
  full lingua mode (~1 GB)

### Acceptance evaluation

The Phase-1 acceptance harness lives at `tests/acceptance/run_recall_eval.py`.
It seeds a synthetic multilingual corpus, runs the embed worker, then evaluates
recall@K and MRR@K against a ground-truth query set:

```bash
LOCALMAIL_TEST_DSN=postgresql://... \
  PYTHONPATH=src:. uv run python tests/acceptance/run_recall_eval.py \
  --queries tests/fixtures/multilingual_queries.json \
  --k 20
```

Phase-1 gates: recall@20 >= 80% and MRR@20 >= 0.5 for de/en/es/ja.
Norwegian is reported but not gated. Author 20 queries per language in
`tests/fixtures/multilingual_queries.json` (see the `.example.json` for
format) before running the gate.

A companion sweep harness at `tests/acceptance/run_rrf_k_sweep.py` seeds
the corpus once and re-evaluates the query suite for each candidate
`rrf_k`, so the expensive embed pass is not repeated. Pass `--corpus
attachment` to exercise arm 4. The current default (`rrf_k=60`) was
verified against both synthetic corpora in #35 — see that issue for
the full table.

`tests/acceptance/run_chunk_insert_bench.py` times the chunking loop's
row-by-row vs batched-`executemany` chunk INSERTs. The #5 measurement
showed the loop is tokenization-bound (not INSERT-bound), so the
production loop stays row-by-row — see that issue for the full table.
