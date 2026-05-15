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

# 5. Run a one-shot sync (cron-friendly)
uv run localmail sync
uv run localmail sync --account horst-gmail
```

Daemon mode (IMAP IDLE on INBOX + periodic poll on other folders) is the
intended long-running mode but is not yet implemented — track step 7 of the
build plan.

## CLI

| Command | Purpose |
| --- | --- |
| `localmail init-db` | Apply pending schema migrations. Idempotent. |
| `localmail list-accounts` | Show configured accounts and whether a secret is stored. |
| `localmail add-account NAME` | Prompt for an IMAP password and store it in the keyring. |
| `localmail oauth-login NAME` | Run the Gmail OAuth desktop consent flow. Stores the refresh token in the keyring. |
| `localmail remove-account NAME` | Drop any stored secrets (password + refresh token) for an account. |
| `localmail sync [--account NAME] [--no-ssl]` | One-shot incremental sync. |

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
   name           = "horst-gmail"
   email          = "you@gmail.com"
   imap_host      = "imap.gmail.com"
   auth_method    = "oauth2"
   oauth_provider = "gmail"
   folder_deny    = ["[Gmail]/All Mail", "[Gmail]/Trash", "[Gmail]/Spam"]
   ```

6. **Run the consent flow once**:

   ```bash
   uv run localmail oauth-login horst-gmail
   ```

   This opens a browser, you grant access, and the refresh token is written
   to the keyring. Sync uses the refresh token to mint short-lived access
   tokens — no further interaction required unless you revoke access at
   <https://myaccount.google.com/permissions>.

### Why deny `[Gmail]/All Mail`?

Gmail surfaces every message under both its INBOX/label folders *and* under
`[Gmail]/All Mail`. localmail dedups by Message-Id per account, so the same
message in INBOX and All Mail produces one `messages` row with two
`message_labels` rows. That's fine, but `All Mail` adds no new information.
Excluding it roughly halves the upfront sync time.

## Development

```bash
uv sync
uv run pytest                # 36 tests; needs Postgres reachable at TEST_DSN
uv run localmail --help
```

`tests/conftest.py` will auto-skip DB-dependent tests if no Postgres is
reachable at `LOCALMAIL_TEST_DSN` (defaults to
`postgresql://localmail:local%40%40mail@localhost:5532/localmail`).
