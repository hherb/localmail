# localmail

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

## Development

```bash
uv sync
uv run pytest                # full suite (~56 tests); skipped if no Postgres
uv run localmail --help
```

`tests/conftest.py` auto-skips DB-dependent tests if no Postgres is reachable
at `LOCALMAIL_TEST_DSN` (defaults to
`postgresql://localmail:local%40%40mail@localhost:5532/localmail_test` — a
separate database from the live archive, so tests can't clobber real data).
