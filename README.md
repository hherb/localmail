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
  a single TOML file.

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

| Command | Purpose |
| --- | --- |
| `localmail init-db` | Apply pending schema migrations. Idempotent. |
| `localmail list-accounts` | Show configured accounts and whether a secret is stored. |
| `localmail add-account NAME` | Prompt for an IMAP password and store it in the keyring. |
| `localmail oauth-login NAME` | Run the Gmail OAuth desktop consent flow. Stores the refresh token in the keyring. |
| `localmail remove-account NAME` | Drop any stored secrets (password + refresh token) for an account. |
| `localmail sync [--account NAME] [--limit-per-folder K] [--no-ssl]` | One-shot incremental sync. |
| `localmail run [--log-level …] [--no-ssl]` | Foreground daemon: per-account IDLE thread on INBOX + periodic poll thread for other folders. SIGTERM/SIGINT shut down cleanly. |
| `localmail list-failed [--account NAME] [--limit K]` | Show messages that sync skipped due to errors. |
| `localmail retry-failed [--account NAME]` | Re-attempt every failed message. Successful retries move from `failed_messages` to `messages`. |

### Search backfill & status

| Command | Purpose |
| --- | --- |
| `localmail embed-backfill` | Drain the message-chunk embedding queue in the foreground; exit when empty. |
| `localmail extract-backfill [--no-progress]` | Drain the attachment-extraction queue (Phase 2): extract text from PDFs, DOCX, etc. |
| `localmail search "QUERY" [--format text\|json]` | Hybrid lexical + vector search over the local archive (see [Search](#search) below). |
| `localmail search-status [--format text\|json]` | Report chunk/extraction backlog and failure counts for Phase 1 and Phase 2. |
| `localmail list-failed-embeddings` | Show recent `failed_embeddings` rows. |
| `localmail retry-failed-embeddings` | Clear `failed_embeddings` so the embed worker re-picks them up. |
| `localmail list-failed-extractions` | Show recent `failed_extractions` rows. |
| `localmail retry-failed-extractions` | Clear `failed_extractions` so the extract worker re-picks them up. |

### GUI server (HTTPS API)

| Command | Purpose |
| --- | --- |
| `localmail serve [--bind 127.0.0.1] [--port 8443] [--tls-cert PATH] [--tls-key PATH] [--no-tls]` | Run the HTTPS API server. TLS is mandatory unless `--bind 127.0.0.1 --no-tls`. |
| `localmail add-api-user USERNAME [--password TEXT \| --password-stdin]` | Create an API user (argon2id-hashed password). |
| `localmail list-api-users` | List configured API users. |
| `localmail remove-api-user USERNAME` | Delete an API user and all its tokens. |
| `localmail rotate-tls --cert PATH --key PATH [--hostname H] [--force]` | Generate (or regenerate) a self-signed TLS cert + key. |

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
uv run pytest                # full suite (~400 tests); skipped if no Postgres
uv run localmail --help
```

`tests/conftest.py` auto-skips DB-dependent tests if no Postgres is reachable
at `LOCALMAIL_TEST_DSN` (defaults to
`postgresql://localmail:local%40%40mail@localhost:5532/localmail_test` — a
separate database from the live archive, so tests can't clobber real data).

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
# weights to ~/.cache/fastembed/ (one-time).
uv run localmail embed-backfill

# (Optional, Phase 2) Backfill attachment text for an existing archive.
# Requires the docling optional dep: `uv sync --extra extraction`.
uv run localmail extract-backfill

# Progress at any time:
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
`lang:XX` (ISO 639 code; matches the `messages.body_lang` column populated
per-message), and `has:attachment`. Each operator may appear multiple times
where it makes sense (e.g. multiple `lang:` accumulate).

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
- `rerank_pool_size` (default 50) — match `candidates_per_arm`
- `chunk_size_tokens` (default 512) — smaller for short messages

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
