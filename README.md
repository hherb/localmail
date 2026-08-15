# localmail

<p align="center">
  <img src="assets/banner medium.png" alt="localmail" width="480">
</p>

Mirrors one or more IMAP accounts (password or Gmail OAuth2) into a local
PostgreSQL database. The archive is **read-only with respect to upstream**:
localmail never deletes, modifies, or sends mail. Downstream agents read from
Postgres and the attachment tree without touching IMAP.

> **End-user docs:** this README is the operator/developer reference. For a
> step-by-step guide aimed at end users — setup, the daemon, the CLI, the
> browser-based **admin UI** (with screenshots), importing existing mail, and
> AI-agent access — see the [user manual](docs/manual/users/index.html).

## Layout

- Email rows + headers + raw RFC822 + extracted plaintext/HTML live in Postgres.
- Attachments are stored content-addressably at
  `<attachments.root>/blobs/<aa>/<bb>/<full-sha256-hex>` — identical bytes are
  written exactly once across the whole archive regardless of how many emails
  or accounts reference them. Each message's `attachments` JSONB column
  records `[{"filename": "<original-name-from-this-email>", "sha256": "<hex>"}, …]`,
  preserving the *per-email* filename so files can be restored with the names
  they had when received.
- Secrets (IMAP passwords, OAuth refresh tokens) live in the OS keyring by
  default — macOS Keychain on darwin, Secret Service (gnome-keyring / KWallet)
  on Linux — or in a 0600 file for headless hosts. See
  [Headless secret storage](#headless-secret-storage).
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

Two **global** options go *before* the subcommand — `localmail --config PATH
<command>`, not `localmail <command> --config PATH`, which is an error.

`--config PATH` overrides the config file. The default is `$LOCALMAIL_CONFIG`,
else `$XDG_CONFIG_HOME/localmail/config.toml`, else
`~/.config/localmail/config.toml`.

`localmail --version` prints the installed version; it reads no config and
touches no database, so it still answers on a half-set-up host. It is the only
`localmail` command that reports the version — on a machine running just the
sync daemon, `/v1/version` would mean starting `serve`.

If the version cannot be read at all it prints `0.0.0+unknown` and explains why
**on stderr**, naming the exact command to run. There are three causes and the
remedies differ: nothing is installed (`uv sync`, or `uv tool install
localmail`); the install is damaged, which needs reinstalling *over* what is
there rather than adding to it; or the metadata could not be read at all. That
third case prints a `cause:` line naming the exception — read it before acting,
because the catch behind it is deliberately broad (an import must never fail
here) and also sees failures that are not about the file at all. An `OSError`
there means checking the filesystem under `site-packages` first, since no
reinstall fixes a failing mount. When the exception was raised *from* another
one, the cause line follows the chain — `RuntimeError: finder failed <- caused
by OSError: [Errno 5] …/METADATA` — because the errno and the filename you act
on are usually in the inner one. stdout stays the single machine-readable
version line, so scripts that parse it are unaffected, and the exit status
stays `0` — the stderr line is the failure signal, so do not consume stdout
alone when you are verifying an install.

**Every command reports it, not just `--version`.** A broken install is
announced once by whichever `localmail` command you run, before it reads
`config.toml` and before it touches Postgres — so a host that is broken in more
than one way still tells you about its version first, and a nightly cron
`localmail sync` on a host whose `site-packages` mount has started failing says
so instead of exiting 0 in silence. `serve` and `run` report through their own
loggers at **ERROR**, once per process, so a headless deployment surfaces it
where its operator actually looks. ERROR rather than warning because `run
--log-level ERROR` is a supported choice and a report you can be configured out
of is not a report; the line's own `error:` prefix is derived from that level,
so it still carries a severity on the paths that print no level. Nothing is
logged when the version reads normally, and `/v1/version` reports the same
four outcomes as a machine-readable `version_source` (see below).

For scripts, the contract is: **stderr is non-empty if and only if the version
could not be resolved.** (This holds for what `localmail` itself writes; a
dependency emitting an import-time warning would also land on stderr, so a
script that must be certain should read `version_source` from `/v1/version`
instead.) stdout stays the single `localmail, version X.Y.Z`
line and the exit status stays `0` in both cases, so neither is a failure
signal — check stderr, or read `version_source` from `/v1/version`, which
reports the same four outcomes as a machine-readable string.

`GET /v1/version` (unauthenticated) reports six fields: `api_major`,
`api_minor`, `server_version`, `build_hash`, `build_source`, `version_source`.

`build_hash` is the short git SHA of the checkout the server is running,
suffixed `-dirty` when tracked files differ from it — the answer to "did the
daemon get restarted after my pull?". It is `null` when there is no identity to
report, and `build_source` says why: `git_checkout`, `stamped`, `not_a_repo`,
`git_unavailable`, `git_failed`.

`version_source` is `installed` on a healthy install, and `not_installed`,
`metadata_incomplete` or `metadata_unreadable` when `server_version` is the
`0.0.0+unknown` sentinel — so a monitoring client can alert on a broken install
rather than displaying the sentinel as though it were a version. Both source
fields are always present; only `build_hash` is nullable.

Asking for **help** is the one exception: `localmail <command> --help` stays
quiet, as bare `localmail` and `localmail --help` already did. Help does no
archive work and touches neither config nor database, and the line was landing
ahead of the text you had just asked to read. Use `localmail --version` when
what you want *is* the state of the install.

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
| `localmail add-account NAME` | Prompt for an IMAP password and store it in the configured secret store. Resolves `NAME` against the DB; if absent but declared in `config.toml`, the DB row is created from that block first. |
| `localmail oauth-login NAME` | Run the Gmail OAuth desktop consent flow. Stores the refresh token in the configured secret store. Resolves `NAME` against the DB (seeding from `config.toml` if absent). |
| `localmail remove-account NAME [--delete-row] [--force]` | Clear stored secrets for an account. `--delete-row` also removes the DB account row (`--force` cascades when messages reference it). |
| `localmail enable-account NAME` / `localmail disable-account NAME` | Resume or pause syncing for an account by flipping `sync_enabled` in the DB. A paused account spawns no daemon threads; a one-shot `localmail sync --account NAME` still runs it. Archive accounts are rejected; re-running on an account already in the target state is a no-op. |
| `localmail sync [--account NAME] [--limit-per-folder K] [--no-ssl]` | One-shot incremental sync over the syncable database accounts (live + `sync_enabled`). `--account NAME` syncs one account even if it is paused (`sync_enabled = false`); archive accounts are rejected. |
| `localmail run [--log-level …] [--no-ssl]` | Foreground daemon: per-account IDLE thread on INBOX + periodic poll thread for other folders. **Hot-reloads accounts** — add/remove/pause/resume an account or rotate its credentials and the running daemon converges within `[daemon] reload_seconds` (default 30 s), no restart needed. SIGTERM/SIGINT shut down cleanly. If Postgres is briefly unreachable at launch, startup retries with bounded exponential backoff (`[daemon] startup_backoff_initial_s`→`startup_backoff_max_s`, default 1→60 s) rather than crashing; the daemon's fresh (non-pool) connects — startup account read, reconcile, heartbeat clear — are bounded on every phase so no network fault stalls startup or hot-reload for the OS TCP default: the TCP connect by `[daemon] db_connect_timeout_s` (default 10 s), server-side query execution by `[daemon] db_statement_timeout_s` (default 30 s, `0` disables — catches a slow/stuck query), and a post-connect black-hole (packets dropped *after* connect) by `[daemon] db_tcp_user_timeout_ms` (default 30000 ms, `0` = OS default; libpq `tcp_user_timeout`, Linux-effective, ignored on platforms without `TCP_USER_TIMEOUT`). The workers' **IMAP** calls are bounded the same way by `[daemon] imap_timeout_s` (default 60 s) — without it a network black-hole blocks a worker forever, pinning its pool connection and leaving it deaf to the stop signal. It is a *per-recv* bound, so a slow but progressing download is safe; raise it if an account gets stuck reconnecting, since a server-side stall with nothing on the wire (a Gmail `SEARCH` over a very large `\All` folder) is indistinguishable from a black-hole. IDLE waits use their own bound and are unaffected. The daemon records per-thread liveness heartbeats in the `daemon_heartbeats` table — covering each account's IDLE + poll threads plus the embed/extract/reconcile process workers — and a heartbeat is considered stale after `[daemon] heartbeat_stale_seconds` (default 120 s); the admin daemon-status endpoint (2B.4) exposes this liveness state. The running daemon also drains a `daemon_commands` queue at the top of each reconcile tick — `reload-now` (converge immediately instead of waiting out `reload_seconds`), `restart-account` (tear down + respawn one account's threads, e.g. for a wedged connection), and `drain-stop` (graceful shutdown) — and `LISTEN`s for an enqueue `NOTIFY` so a queued command wakes the loop at once rather than on the next poll (disable the listener with `[daemon] command_listen_enabled = false`; its poll interval is `[daemon] command_listen_poll_seconds`, default 5 s). Enqueue these commands via the `localmail daemon` CLI subgroup or the admin HTTP routes (2B.4). |
| `localmail list-failed [--account NAME] [--limit K]` | Show messages that sync skipped due to errors. |
| `localmail retry-failed [--account NAME]` | Re-attempt every failed message. Successful retries move from `failed_messages` to `messages`. |
| `localmail list-failed-fetches [--account NAME] [--limit K]` | Show messages whose body the server never handed over (sync gave up on them). |
| `localmail retry-failed-fetches [--account NAME] [--forget] [--older-than-days N] [--dry-run]` | Rewind the affected folders so the next sync re-fetches them; `--forget` drops the records instead. Records clear when the message arrives, so the command is safely re-runnable. |
| `localmail migrate-secrets` | Copy every account's secrets from the OS keyring into the file backend, for headless hosts. `--dry-run` previews. See [Headless secret storage](#headless-secret-storage). |
| `localmail sweep-blob-temps [--dry-run] [--max-age-seconds S]` | Delete attachment temp files a hard kill stranded in the blob tree. |

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
| `localmail daemon start` / `stop` / `restart` [`--no-wait`] | **Plane B.** Drive the supervised child over the control socket. The supervisor runs the lifecycle op on its own thread and returns at once, so the command **polls status until the daemon settles** (running / stopped) — `--no-wait` skips the poll and prints the transitional state. Exits non-zero with a clear note when the daemon is supervised externally (`supervise_daemon = false`), when `localmail serve` is not running, or when the supervisor accepts the connection but stops answering. |

#### How long a stop takes

`[daemon] shutdown_grace_seconds` (default 30) is the **total** wall-clock
budget the daemon spends winding its worker threads down, not a per-thread
timeout: every worker is signalled first and they wind down concurrently, so
the budget bounds the slowest one rather than their sum. If it is exhausted the
daemon logs a warning naming it, which is the signal to raise it.

The supervisor deliberately waits **longer** than that before escalating
SIGTERM to SIGKILL — the child is still closing its connection pool and exiting
when its last join returns, so killing it at that exact instant would turn
every ordinary stop into a SIGKILL. You do not configure the difference.

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

Importing **two sources whose filenames share a stem** into the same account is
also safe. Folders are named after the source's filename stem, so
`2023/Inbox.mbox` and `2024/Inbox.mbox` both land in a folder called `Inbox`;
each import continues numbering where the previous one left off. Before this was
fixed (#215) the second import restarted its numbering and every one of its
messages was rejected into `failed_messages` — not because anything was wrong
with them, and with no way to retry successfully.

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
| `localmail lang-backfill [--no-progress] [--retry-declined] [--relabel [--yes]]` | Populate `messages.body_lang` for every message with NULL body_lang. Required once after first install so the `lang:` search token returns rows. `--retry-declined` first re-opens rows a stricter detector policy turned away; `--relabel` discards **every** existing label and re-detects the archive (prompts unless `--yes`). |
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
| `localmail revoke-admin-sessions USERNAME` | Invalidate every credential `USERNAME` holds — admin cookie sessions, `/v1` + `/mcp` bearer tokens, OAuth refresh tokens, and in-flight authorization codes. Credentials issued *after* the command still work, so "revoke, then log in again" is the recovery path. Admin privilege itself is untouched — use `revoke-admin` for that. |

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
   It also **may not contain `:`**: the keyring stores an account's IMAP
   password under `<name>` and its OAuth refresh token under `<name>:refresh`,
   so a name like `gmail:refresh` would address another account's token slot.
   Blank and over-long (>128 char) names are rejected too. The same rule
   applies when creating an account from the admin UI, the JSON API, or the
   CLI — names are fixed at creation, so renaming means recreating.

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

For rows belonging to an **archive** account, retry assigns a fresh message
number rather than reusing the stored one. Imported numbers are synthetic, so a
row left behind by the pre-#215 collision bug would otherwise clash forever and
never recover. Live IMAP accounts keep their stored UID exactly — there it is
the server's own identifier and a clash means something genuinely wrong, which
should surface rather than be papered over.

**Messages the server won't hand over.** If a fetch returns an empty body,
localmail asks the server whether the message is still there. If it has been
deleted meanwhile, sync moves past it. If it is still present — a server hiccup
— sync deliberately does *not* advance, so the next run tries again instead of
losing the message silently. Because a message can be permanently unfetchable
(a zero-length message, a corrupt entry on the server), that retrying stops
after `[daemon] max_body_fetch_hold_s` (default 1800 s), at which point one
distinct *"giving up"* WARNING is logged and sync moves on. A successful fetch
resets the window, so it measures one **continuous** outage; `0` disables the
retrying entirely.

It is a duration rather than an attempt count on purpose. The IDLE thread
re-syncs INBOX on *every* notification — including another mail client merely
toggling a flag — so a count would be spent at your mailbox's traffic rate: five
notifications in ten seconds would exhaust a five-attempt budget and drop a
message over a blip that resolved a minute later, while the slower folder poll
got half an hour from the same number.

Giving up leaves a **record**, not just a log line — that message is genuinely
absent from your archive, and you should be able to find it:

```bash
uv run localmail list-failed-fetches                 # what sync gave up on
uv run localmail retry-failed-fetches --dry-run      # what a re-fetch would rewind
uv run localmail retry-failed-fetches                # rewind and re-fetch
uv run localmail retry-failed-fetches --forget       # accept the loss, drop the records
```

Retry rewinds each affected folder's resume point to the lowest given-up
message number, so the next sync reaches it again. Everything above that point
is re-scanned; already-archived mail is skipped, so it is always safe — but it
is not always cheap. If the message is *still* unfetchable (often the case —
that is why sync gave up), it takes a fresh hold on the folder's resume point,
so the re-scan repeats on every pass until that hold runs out after
`[daemon] max_body_fetch_hold_s` (default 30 min). On INBOX, where the daemon
re-syncs on every new-mail notification, that can be a lot of passes. Run
`--dry-run` first: it prints how far back each folder would be rewound, which
is the whole cost estimate.

A record is **kept** through the retry and clears only when the message
actually arrives — until then it really is still missing, so it stays listed.
That also makes the command safely re-runnable: if the sync daemon happened to
be mid-pass on that folder, it can carry the resume point forward again, and
re-running is the entire remedy.

There is no automatic expiry of these records: they are the only trace of
permanently lost mail, so removing them is your call (`--forget`, optionally
`--older-than-days N`).

### Orphaned attachment temp files

Attachments are written to a private temp name and then atomically renamed into
the content-addressed blob tree. A hard kill — SIGKILL, the OOM killer, power
loss — landing between those two steps leaves the temp behind, and nothing
reuses it. The sync daemon collects them at startup; you can also run it
directly:

```bash
uv run localmail sweep-blob-temps --dry-run   # report what would be reclaimed
uv run localmail sweep-blob-temps             # reclaim it
```

Only files older than `[attachments] temp_max_age_s` (default 24 h) are
removed, which is what guarantees a temp an in-flight writer still owns is
never touched. A real attachment write takes milliseconds, so there is no
reason to lower it — and since that margin is the only protection, the setting
is floored at one second.

### Which attachments get extracted

A blob is extracted when **either** its declared MIME type is in
`search.extractor_mime_allowlist` **or** one of the original filenames it was
received under has an extension in `search.extractor_extension_allowlist`.
Either match suffices because senders get this wrong in both directions — a real
PDF routinely arrives as `application/octet-stream` from a mobile client.

The extension comes from the filename recorded in `messages.attachments`, never
from the on-disk blob path: that path is content-addressable
(`blobs/<aa>/<bb>/<sha256hex>`) and has no extension at all.

A blob neither allowlist admits gets an `attachment_text` row with
`extractor = 'type-skipped'` and empty text, so the decision is queryable:

```sql
SELECT b.mime_type, count(*) FROM attachment_text t
  JOIN attachment_blobs b USING (sha256)
 WHERE t.extractor = 'type-skipped' GROUP BY 1 ORDER BY 2 DESC;
```

That row is also what makes the blob ineligible for the next claim. If you widen
an allowlist and want the skipped blobs reconsidered, delete the rows first —
nothing does it automatically:

```sql
DELETE FROM attachment_text WHERE extractor = 'type-skipped';
```

### Reading the attachment counters

`search-status` divides the **eligible** blobs — the ones an allowlist admits,
see [Which attachments get extracted](#which-attachments-get-extracted) — into
four counts that add up to `blobs_eligible`, so the four together account for
every eligible blob:

| Count | Meaning |
|---|---|
| `blobs_extracted` | Text was extracted and stored. |
| `blobs_no_text` | Processed, but produced no text: skipped by size or type, extracted to nothing, or whitespace-only. |
| `blobs_gave_up` | Not processed — a retry budget ran out. |
| `blobs_pending` | Outstanding work the extract worker will still pick up. |

Only `blobs_pending` is a backlog, and it does reach zero. A steady non-zero
`blobs_no_text` is **normal** — those blobs are finished, just with nothing to
index — so treat it like `body_lang_declined`. Break it down by cause with:

```sql
SELECT extractor, count(*) FROM attachment_text
 WHERE extracted_text = '' GROUP BY extractor ORDER BY 2 DESC;
```

`blobs_gave_up` is the one to act on. `localmail retry-failed-extractions` puts
them back in the queue. `localmail list-failed-extractions` shows why for the
poison-pill half only — a blob parked by the *transient* budget (a broken OCR
engine, an expired Hugging Face token) writes no `failed_extractions` row and
has no list command, so query it directly:

```sql
SELECT * FROM transient_extractions ORDER BY last_transient_at DESC;
```

### `blobs_claimable`: the number that is not in the table

The four counts above cover eligible blobs. The extract worker's queue is
**wider than that** — it claims every unprocessed blob and applies the
allowlist afterwards, disposing of a miss with a `type-skipped` row. So a blob
outside both allowlists is real work the worker will do, and it appears in none
of the four counts.

`blobs_claimable` is that full queue depth, and `blobs_pending` is its
allowlisted subset. On an archive that is mostly images, `blobs_pending: 0`
alongside `blobs_claimable: 16000` is the honest reading: nothing left to
*index*, but hours of claiming and skipping still to go. If the two are far
apart and stay that way, widening `extractor_mime_allowlist` — or accepting
the skips — is the decision to make.

### Scanned PDFs and OCR

A PDF whose pages are images carries no text stream, so the pure-Python
extractor gets nothing and the docling fallback takes over. docling can OCR
those pages, but it **ships no OCR engine of its own** — one has to be
installed alongside it.

`search.extractor_ocr_engine` (default `"auto"`) decides which:

| value | behaviour |
|---|---|
| `"auto"` | Use whichever engine is installed (docling probes ocrmac → rapidocr → easyocr). If none is, pages pass through un-OCR'd and the PDF is recorded as `lightweight-empty` — no error. Install an engine later and OCR starts working with no config change. |
| `"none"` | Skip OCR entirely. docling still contributes layout and table-structure analysis, so the fallback keeps some value. |
| an engine name | Pin one: `easyocr`, `ocrmac`, `rapidocr`, `tesseract`, … Validated against what your docling build actually registers. |

**With the `[extraction]` extra installed, `"auto"` finds a working engine on
both platforms and there is nothing else to configure.** On macOS the extra adds
**ocrmac** (a thin Apple Vision wrapper). On Linux, **rapidocr** arrives as a
dependency of `docling[standard]` and runs on `onnxruntime`, which is already a
core localmail dependency via fastembed. Verified on both: macOS logs
`Auto OCR model selected ocrmac`, Linux logs
`Auto OCR model selected rapidocr with onnxruntime`.

Neither path uses easyocr, so neither pulls torch *for OCR*. (On Linux, docling
itself may still pull torch as its own dependency — on an aarch64 host with CUDA
that is roughly 5 GB of venv. The OCR engine does not use it.)

Pin an engine only to override that choice — e.g. `"none"` to skip the OCR cost
on a large archive, or a specific engine to force it.

An engine that is *named* but not importable, or a name docling does not
register, is a **configuration** error: one WARNING naming the problem, and the
blob's extraction is **held** on the transient counter rather than failed, so
`retry_count` is never spent on a problem no retry can fix. Fix the config and
run `localmail retry-failed-extractions` to release them.

### How extraction failures are classified

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
- **Configuration** — an OCR engine that is named but not installed, or a name
  docling does not register (#248). Not the blob's fault and not fixable by
  retrying, so it is held on the same transient counter and **never** burns
  `retry_count`; one WARNING per process names the engine and the way out.
- **Poison-pill** — corrupt PDF, encrypted, parser raise, anything else.
  Recorded in `failed_extractions` with `retry_count += 1`, permanently
  skipped once `retry_count >= search.extract_worker_max_retries` (default 3).

Extracted text is stripped of NUL bytes before it is stored (#249). Postgres `TEXT`
rejects `\x00`, and a document that contains one would otherwise fail its
INSERT and be recorded as a poison-pill under the extractor name `unexpected` —
permanently, since the same bytes always re-extract to the same NUL.

```bash
uv run localmail list-failed-extractions      # show recorded poison-pills
uv run localmail retry-failed-extractions     # clear failed + stuck-transient state so they re-queue
```

## Headless secret storage

By default IMAP passwords and Gmail refresh tokens go in the OS keyring, which
is the right place on a desktop: your login unlocks it.

**On a headless host it cannot work.** A lingering systemd *user* service starts
at boot with no PAM session, and the gnome-keyring `login` collection is
unlocked by PAM at interactive login and by nothing else. So the collection is
locked, every read raises `KeyringLocked`, the daemon dies, `Restart=always`
brings it back, and it dies again — until someone SSHes in and unlocks it by
hand. That is not a misconfiguration to fix; it is what a login keyring is.

Point localmail at a file instead:

```toml
[secrets]
backend = "file"
# file_path = "~/.config/localmail/secrets.json"   # this is the default
```

The file is written 0600 through an atomic rename, so a reader never sees a
partial write and the secret is never briefly world-readable. **File permissions
are the only protection**, deliberately — the disk is already encrypted, and
anyone who can read a 0600 file owned by the service user is already that user
or root. If the mode is ever found readable by group or other, reads are
**refused** with the exact `chmod` to run rather than quietly using it.

The *directory* is graded too, because write access to a directory allows
renaming the entries inside it whatever their own modes — so a writable parent
lets somebody swap the 0600 file for one of their own and the mode check still
passes. A **world-writable** parent is refused (`chmod o-w`); a
**group-writable** one logs one warning per process and carries on, since a
directory you created under the common umask-002 + private-group setup lands at
0775 with your own group and is safe in practice. Read and execute bits are
ignored — `~/.config` is routinely 0755, and listing a directory reveals only
the file's name.

To move an existing install without re-driving the Gmail consent flow on a
machine with no browser, unlock the keyring one final time and copy across:

```bash
uv run localmail migrate-secrets --dry-run   # show what would move
uv run localmail migrate-secrets             # copy keyring -> file
# then set backend = "file" and restart
```

The keyring is left intact, so the migration is re-runnable and reversible by
just switching `backend` back.

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

### Server-side polling cursors

A client that keeps its own `since` cursor re-reads the 200-message tail
whenever it restarts. That is harmless for a mail UI, but not for a
consumer that turns each new message into work — it would replay old
mail. Such a client can stay stateless by letting the server hold the
cursor:

```
GET  /v1/changes?subscription=my-agent      → {"new_messages": [...], "next_cursor": "1234"}
POST /v1/changes/ack  {"subscription": "my-agent", "cursor": "1234"}   → 204
```

Poll, process, ack. Semantics:

- `subscription` and `since` are **mutually exclusive** (400) — pick a
  server-side or a client-side cursor, not both.
- **First use of a name starts at the current tip**, so the first poll
  returns nothing and a new subscriber never replays history. Poll once
  at startup to establish the subscription before you need it.
- Cursors are scoped per api-user *and* per name, and are **only**
  advanced by an ack — polling never advances them, so a crash between
  poll and ack redelivers rather than drops.
- **Acks never rewind.** A stale or replayed ack is a no-op, so retries
  are safe. For the same reason a cursor past the newest message is
  rejected (400) instead of silencing the subscription: only ack a
  `next_cursor` you were actually given.
- A name is 1–64 chars of `[A-Za-z0-9_-]`, capped at
  `[serve] max_subscriptions_per_user` (default 32) distinct names per
  user. Use a small set of stable names, not one per process or run.

`POST /v1/search` supports two cursor flavours, transparently:

- **Pool cursor** (`"<token>:<page>"`) — paged result from the
  hybrid retrieval pool. When the page advances past the cached pool
  and `can_grow_pool=true`, the route doubles `candidates_per_arm` up
  to `candidates_per_arm_max` (default 800), then flips
  `next_cursor` to `null`.
- **Keyset cursor** (`"K|<base64>"`) — used for `sort=date` +
  non-empty query, backed by a lexical FTS scan over
  `COALESCE(internal_date, date_sent)`. Unbounded scroll; no pool
  cap. Same recall as the lexical retrieval arm.

When paging, send the cursor back with the same `query` and filters and
**leave `sort` unset**. The cursor already carries the ordering it
continues, so a stated `sort` that contradicts it is a 400 rather than a
silent restart at page 1 of a differently ordered search. A keyset cursor
also needs the query re-sent — it rebuilds the lexical walk from it — and
is rejected without one.

If a paged cursor's underlying pool was evicted from the in-memory
cache (TTL expiry, LRU eviction, or `serve` restart) the route
returns HTTP 409 with `type: /problems/search-cursor-expired`. The
GUI handles this transparently by re-issuing the original query
without a cursor and skipping past rows it already holds.

The two failures are different kinds and want different client handling. A
**409** is recoverable — the request was well formed and only the pool is gone,
so re-running it without a cursor continues where the user was. A **400** is
permanent for that cursor: re-issuing the identical pair cannot succeed, so a
client must retire the cursor rather than let infinite scroll re-fire it behind
an error banner. The desktop GUI does both, and never states a `sort` on a
request that carries a cursor — which is what makes the contradicting-sort 400
unreachable from it rather than merely handled.

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

A spec-strict client can also **discover** that `/mcp` is a protected resource
(RFC 9728): an unauthenticated request gets a `401` whose `WWW-Authenticate`
challenge points at `/.well-known/oauth-protected-resource/mcp`, served at the
origin root. By default localmail is not an OAuth authorization server —
discovery only advertises the resource; the token is obtained out-of-band via
`/v1/auth/login`. Set `[mcp].resource_server_url` to the externally reachable
origin so the challenge and metadata are correct behind a proxy.

For zero-config MCP client onboarding localmail can optionally act as an **OAuth
2.1 authorization server**: enable it with `[mcp] authorization_server_enabled =
true` plus `[serve] state_signing_key = "<>=32 chars>"` (required; `serve` fails
loud if absent). The operator sets only `[mcp] resource_server_url`; the AS
issuer and all OAuth endpoints are auto-derived as `<resource_server_url>/mcp`.
A spec-strict client (e.g. Claude Desktop) then self-onboards via Dynamic Client
Registration, a browser login + consent screen (the user logs in with their
existing api_user credentials — no new user accounts are created), and PKCE
code exchange. Access tokens are stored in the existing `api_tokens` table, so
the per-user account ACL and `localmail grant-account` grants are unchanged.
Tokens auto-refresh; a browser re-login is needed only after ~30 days of
inactivity, on revocation (`localmail revoke-admin-sessions` cuts off access
tokens, refresh tokens **and** any authorization code still mid-flight), on
detected refresh-token reuse (a rotated token
replayed → the whole refresh chain is revoked **and the access tokens issued
from it are purged immediately** rather than lingering until expiry, RFC 9700
§4.14.2), or if the user is disabled. localmail also validates the RFC 8707
`resource` indicator at `/authorize` and binds the issued access/refresh
tokens' audience to `<origin>/mcp`, enforced on every `/mcp` request
(`/v1` REST is unaffected); set `[mcp] oauth_require_resource_indicator =
true` to make the `resource` parameter mandatory rather than optional. See
[docs/mcp-usage.md](docs/mcp-usage.md) for the
full operator guide including token lifetimes, DCR safeguards, and known
limitations.

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

**Admin mode.** When the logged-in user has `is_admin`, the header shows an
**Admin** button opening an admin overlay, driven over the same bearer token
the viewer already uses (no cookie, no CSRF; a native client carries no
ambient credential). Two panels are built:

- **Accounts** — account CRUD, pause/resume sync, IMAP password storage, and
  test-connection.
- **Daemon** — a self-refreshing status view (process state, per-worker
  heartbeats, recent log) plus controls, mirroring the web `/admin/daemon`
  panel. Reload and per-account restart-sync work regardless of who owns the
  daemon process; the start/stop/restart lifecycle buttons are disabled when
  the daemon is supervised externally (launchd / systemd), which is the case
  under the recommended two-agent deployment. A rejected control (a busy-guard
  **409**) surfaces as a visible message, never an inert button.

Users and Imports panels are not built yet; use the web admin at `/admin/*`
for those. Gmail **Connect** (OAuth) is likewise still web-only.

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
# pre-body_lang archive).
uv run localmail lang-backfill

# After LOWERING body_lang_min_confidence / body_lang_min_text_chars, or
# swapping the detector: re-open the rows the stricter policy declined.
# Without this they stay declined and the looser setting has no effect on
# them.
uv run localmail lang-backfill --retry-declined

# After changing the detector POLICY rather than a threshold (upgrading
# localmail, flipping body_lang_low_accuracy): discard every existing label
# and re-detect. --retry-declined cannot do this — a row carrying a wrong
# label is neither pending nor declined. Prompts unless --yes.
uv run localmail lang-backfill --relabel

# (Optional, Phase 2) Backfill attachment text for an existing archive.
# Requires the docling optional dep: `uv sync --extra extraction`.
uv run localmail extract-backfill

# Progress at any time (body_lang_populated / _pending / _declined):
uv run localmail search-status
```

`search-status` splits unlabelled messages into two counts.
`body_lang_pending` is work the detector will actually claim;
`body_lang_declined` is rows it has already run on and turned away — bodies
below `body_lang_min_text_chars`, detections below `body_lang_min_confidence`,
and bodies that made the detector raise. A steady non-zero `declined` is
normal (separator lines, bare URLs, one-word replies). Only
`--retry-declined` moves rows back from `declined` to `pending`; only
`--relabel` re-opens rows that already carry a label.

The running daemon also detects language on its own, one
`body_lang_detect_batch_size` slice per embed-worker sweep, so a backlog
drains without any of the commands above — it just takes longer, since each
sweep pays a poll interval. Reach for `lang-backfill` when you want a large
backlog cleared now (a fresh archive, or after `--relabel`); leave it to the
daemon otherwise.

Tracking URLs are stripped from the body before detection. Marketing and
newsletter mail is largely tracking links whose path segments are long runs
of high-entropy characters, and a language detector reads that as a
low-resource language: before this was fixed, 17% of all labels on a live
100k-message archive named a language with no plausible presence in it
(#255). A body that is *only* URLs normalises to nothing and is declined
rather than guessed at.

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

### Smart query rewriting (`--smart`, opt-in)

```bash
uv run localmail search "tax stuff from the accountant last summer" --smart
```

`--smart` runs the free-text part of your query through an LLM before searching
(a **local** [Ollama](https://ollama.com) model by default; an OpenAI-compatible
or Anthropic cloud model if you choose — see below). The model is asked to produce
three things:

- a cleaner, semantically richer **rewritten query** for the vector arm,
- a few **synonym/expansion terms** OR-ed into the lexical (keyword) arms to
  broaden recall (capped by `search.rewriter_max_expansion_terms`), and
- **structured filters inferred from natural language** — e.g. "last summer"
  becomes `after:`/`before:`, "from the accountant" can fill `from:`.

**Explicit operators always win.** A filter you typed yourself (`after:`,
`from:`, `subject:`, …) is never overwritten by the model — the LLM only fills
slots you left empty. The model never invents `account:`, `folder:`, or `lang:`
filters.

With the default Ollama backend everything stays on the host: the query text is
sent only to your local Ollama instance, never to a remote service. (If you
switch `rewriter_backend` to `openai` or `anthropic`, the free-text query is
sent to that provider — choose accordingly.) If the LLM is unreachable, the
request times out, or the model returns malformed output, the search **falls
through to the original query** and prints a one-line `note: --smart rewrite
skipped …` (your search still runs — it's just not rewritten).

Setup (default, local Ollama): install Ollama and pull the model once
(`ollama pull granite4.1:3b-q8_0`), then enable the feature in `config.toml`:

```toml
[search]
rewriter_enabled_by_default = true       # build the rewriter at startup
rewriter_backend = "ollama"              # "ollama" (default) | "openai" | "anthropic"
rewriter_model = "granite4.1:3b-q8_0"    # set to match the chosen backend
rewriter_timeout_s = 10.0                # fall through if the LLM is slower
rewriter_max_expansion_terms = 8         # cap on synonyms OR-ed into the lexical arms
ollama_host = "http://localhost:11434"   # must include the scheme
rewriter_cache_size = 128                # cache repeated --smart rewrites; 0 disables
rewriter_cache_ttl_s = 1200              # cache entry lifetime in seconds
```

The base URL of whichever backend you pick (`ollama_host`,
`rewriter_openai_base_url`, `rewriter_anthropic_base_url`) is validated at
startup and must carry an `http://` or `https://` scheme and a host —
`localhost:11434` is **not** enough. A bad value degrades to "no `--smart`"
and logs one line naming the setting, rather than reporting "could not reach
the rewriter service" on every search.

To use a cloud model instead, set `rewriter_backend` and point `rewriter_model`
at that provider's model. The API key is read from an environment variable
(named by `rewriter_openai_api_key_env` / `rewriter_anthropic_api_key_env`,
default `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`) and is **never** written to
config or the database; an unset key degrades to "no `--smart`":

```toml
[search]
rewriter_enabled_by_default = true
rewriter_backend = "anthropic"           # or "openai"
rewriter_model = "claude-haiku-4-5"      # an "openai" example: "gpt-4o-mini"
rewriter_max_tokens = 1024               # cap on generated tokens (cloud backends)
# OpenAI-compatible base URL (includes /v1, per the OpenAI SDK convention):
# rewriter_openai_base_url = "https://api.openai.com/v1"
# Anthropic base URL (origin only; the client appends /v1/messages):
# rewriter_anthropic_base_url = "https://api.anthropic.com"
```

Without `rewriter_enabled_by_default = true`, passing `--smart` reports that no
rewriter is configured. Non-`--smart` search is completely unchanged.

Successful rewrites are memoised in a bounded per-process LRU+TTL cache keyed on
`(today, free_text)`, so a repeated identical `--smart` query skips a fresh
Ollama call (the date is part of the key, so relative dates like "last summer"
re-resolve after midnight). Failures are never cached. Set
`rewriter_cache_size = 0` to disable the cache entirely.

The same rewrite is available over the network read surfaces: `POST /v1/search`
accepts a `smart` boolean in the request body, and the MCP `search` tool a
`smart` parameter. Unlike the CLI — which hard-errors when no rewriter is
configured — the wire endpoints degrade gracefully: an un-rewritten search runs
instead.

Every search response describes what happened to the rewrite via four fields:

- `rewrite_status` — one of `applied`, `unavailable` (smart requested but no
  rewriter configured), `failed` (the rewrite errored), `not_attempted` (a
  continuation page — `smart` applies to page 1 only), or `not_requested`.
- `rewrite_note` — an optional curated, actionable detail (e.g. which
  `ollama pull` fixes a missing model). `null` when there's nothing to say.
- `rewrite_note_code` — a stable, machine-readable partner to `rewrite_note`
  for clients that want to switch on the cause without parsing prose: one of
  `missing_model`, `unreachable`, `unparseable` (the three `failed` causes),
  `not_configured` (`unavailable`), `continuation_page` (`not_attempted`), or
  `null` whenever `rewrite_note` is `null`.
- `rewrite_skipped` — kept for back-compat; `true` only for `unavailable` and
  `failed`.

### Search from Python

```python
from localmail.search import create_searcher

searcher = create_searcher()
# allowed_account_ids is required and has no default: None = no ACL (the whole
# archive), or a list of account ids to scope every retrieval arm to.
page = searcher.search("Berlin conference", allowed_account_ids=None, page_size=20)
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

**Set a persistent model cache on any always-on deployment:**

```toml
[search]
fastembed_cache_dir = "~/.cache/fastembed"
```

fastembed's default cache is `/tmp/fastembed_cache`, and most Linux distros
clear `/tmp` on boot. After a reboot the daemon then re-downloads ~1 GB on
startup — and if it is killed mid-download (a `Restart=always` unit whose
service exits, say), the partially-written cache leaves a snapshot symlink
pointing at a blob that does not exist. Every subsequent start dies with
`onnxruntime … NO_SUCHFILE: Load model … model.onnx failed`, i.e. a restart
loop that never converges. A cache directory outside `/tmp` avoids both the
re-download and the loop.

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
  (and accept more wrong labels); raise to be stricter. Lowering it only
  affects rows the detector has not seen yet; run `localmail lang-backfill
  --retry-declined` to apply it to rows the old floor turned away
- `body_lang_low_accuracy` (default **false**) — lingua's trigram-only mode.
  Measured on a live 100k-message archive it mislabels far more mail while
  costing *more* memory (239 MB vs 227 MB) and running 2.3× slower, so it is
  off by default; set true only on a memory-constrained host
- `embed_worker_idle_backoff_max_steps` (default 6) — how far the background
  worker stretches its poll interval when it finds nothing to do, capping the
  sleep at `embed_worker_poll_interval_s × (1 + steps)` (35 s at the
  defaults). A sweep counts as idle only when *neither* the embedding queue
  nor the language-detection queue advanced. Set `0` to poll at a fixed
  interval

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

## License

localmail is free software, licensed under the **GNU Affero General Public
License v3.0 or later** (`AGPL-3.0-or-later`). See [LICENSE](LICENSE) for the
full text.

The AGPL's network-use clause (section 13) matters here: if you run a modified
localmail as a network-accessible service (e.g. the `serve` HTTP/MCP server),
you must offer the corresponding source of your modified version to its users.

Each source file carries an SPDX header
(`# SPDX-License-Identifier: AGPL-3.0-or-later`) so its license travels with the
file even when copied out of this repository.
