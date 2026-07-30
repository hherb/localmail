# Running localmail persistently on macOS (launchd)

How to make the localmail **sync daemon** (`localmail run`) and, optionally, the
**GUI/API server** (`localmail serve`) start automatically and stay running on a
Mac, surviving reboots.

Both run as **user LaunchAgents**, not root LaunchDaemons. This is mandatory:
IMAP passwords and the Gmail OAuth refresh token live in your **login
Keychain**, which only a process in your GUI (Aqua) login session can read. A
root LaunchDaemon has no access to the unlocked login keychain.

> **"Survives a reboot" means "comes back when you log in after the reboot",**
> not "runs headless before anyone logs in". Because both Postgres and the
> keychain-dependent agents start only in your GUI session, a Mac that reboots
> and sits at the login window will not sync until someone logs in. Truly
> headless operation would require moving secrets out of the login keychain and
> running a root LaunchDaemon — out of scope, and not recommended for a personal
> Mac.

Throughout, substitute your own paths for these examples:

- Repo checkout: `/Users/YOU/src/localmail`
- venv console script: `/Users/YOU/src/localmail/.venv/bin/localmail`
- Config: `~/.config/localmail/config.toml`

## Prerequisites

- **Postgres running and set to auto-start.** localmail is single-host; Postgres
  must be up before it can sync. With Postgres.app, add it to Login Items and
  enable "start server automatically" so the server (not just the app) comes up
  at login. The daemon has built-in startup backoff (1 s → 60 s), so it tolerates
  Postgres coming up a few seconds late.
- **`uv` installed** and the project synced (`uv sync`).
- The archive already configured: `~/.config/localmail/config.toml`, secrets in
  the Keychain (`localmail list-accounts` shows them), and — for `serve` — TLS
  cert/key at `~/.config/localmail/tls/{cert,key}.pem`.

## 1. Dependencies

```bash
cd ~/src/localmail
uv sync --extra mcp     # serve imports localmail.mcp unconditionally
```

> **Gotcha:** `serve` fails at startup with `ModuleNotFoundError: No module
> named 'mcp'` unless the `mcp` extra is installed — even when `[mcp] enabled`
> is false (the OAuth registration guard imports the package at module load).
> A plain `uv sync` **removes** the extra again, which will break `serve` on the
> next launch. Always sync with `--extra mcp` on a serve host (add `--extra
> extraction` too if you use docling). The daemon alone does not need it.

## 2. Migrate the database

Apply any pending migrations to the **existing** archive (safe and additive; it
never drops data, and re-seeds accounts idempotently — existing rows are left
untouched):

```bash
unset VIRTUAL_ENV && ./.venv/bin/localmail init-db
```

`serve` refuses to start while migrations are pending, so always migrate first.

## 3. Configure `[serve]` (only if running the server)

The browser admin UI is mounted **only when both signing keys are set**, and a
launchd-managed daemon must not be supervised by serve. Add to
`~/.config/localmail/config.toml`:

```toml
[serve]
# Admin UI signing keys (>= 32 chars each). Without these, /admin* returns 404
# (the /v1 machine API still works). Generate each with:
#   python -c 'import secrets; print(secrets.token_urlsafe(32))'
session_signing_key = "REPLACE_WITH_A_RANDOM_KEY"
state_signing_key   = "REPLACE_WITH_A_DIFFERENT_RANDOM_KEY"

# launchd owns `localmail run` as its own LaunchAgent, so serve must NOT spawn
# a second daemon. This reports the daemon as "external" in the admin panel;
# reload / per-account restart / read-only status still work via the DB.
supervise_daemon = false
```

Then restrict the file (it now holds secrets) and confirm it loads:

```bash
chmod 600 ~/.config/localmail/config.toml
./.venv/bin/python -c "from localmail.config import load_config; c=load_config(); \
  print('keys', len(c.serve.state_signing_key), len(c.serve.session_signing_key), \
  'supervise', c.serve.supervise_daemon)"
```

serve binds `127.0.0.1:8443` with HTTPS from the default TLS path — no CLI flags
needed. To reach the browser admin UI you also need an **admin user** (see
[Optional: create an admin user](#optional-create-an-admin-user)).

## 4. Log directory

```bash
mkdir -p ~/Library/Logs/localmail
```

(Preferred over `/tmp`, which macOS wipes on reboot.)

## 5. The LaunchAgent plists

Point `ProgramArguments` at the **absolute venv binary** — no PATH or `uv`
resolution at boot. Create `~/Library/LaunchAgents/com.localmail.daemon.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>com.localmail.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/src/localmail/.venv/bin/localmail</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>    <string>/Users/YOU/src/localmail</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>             <string>/Users/YOU/src/localmail/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>          <true/>
  <key>KeepAlive</key>          <true/>
  <key>ThrottleInterval</key>   <integer>30</integer>
  <key>StandardOutPath</key>    <string>/Users/YOU/Library/Logs/localmail/daemon.out.log</string>
  <key>StandardErrorPath</key>  <string>/Users/YOU/Library/Logs/localmail/daemon.err.log</string>
</dict>
</plist>
```

For serve, create `~/Library/LaunchAgents/com.localmail.serve.plist` — identical
but with `Label` = `com.localmail.serve`, the argument `run` replaced by
`serve`, and the log paths pointing at `serve.out.log` / `serve.err.log`.

`RunAtLoad` starts it now and at every login; `KeepAlive` restarts it if it
exits; `ThrottleInterval` bounds crash-restart loops. Agents placed in
`~/Library/LaunchAgents` are auto-loaded at each login — that is what makes them
survive a reboot.

## 6. Load and verify

```bash
UID_NUM=$(id -u)
plutil -lint ~/Library/LaunchAgents/com.localmail.daemon.plist \
             ~/Library/LaunchAgents/com.localmail.serve.plist

launchctl bootstrap gui/$UID_NUM ~/Library/LaunchAgents/com.localmail.daemon.plist
launchctl bootstrap gui/$UID_NUM ~/Library/LaunchAgents/com.localmail.serve.plist
launchctl enable   gui/$UID_NUM/com.localmail.daemon
launchctl enable   gui/$UID_NUM/com.localmail.serve

# Running? A numeric PID in column 1 and exit 0 = healthy.
launchctl list | grep localmail

# Logs
tail -n 40 ~/Library/Logs/localmail/daemon.err.log     # expect: workers started, IDLE
tail -n 20 ~/Library/Logs/localmail/serve.err.log      # expect: serving HTTPS on 127.0.0.1:8443

# serve responding (self-signed cert -> curl -k)
curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1:8443/admin/login   # 200
curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1:8443/v1/accounts    # 401 (auth required)
```

## 7. Gmail OAuth in "Testing" mode expires weekly

localmail's Google OAuth app stays in **Testing** mode. Google expires refresh
tokens for Testing-mode apps after **7 days**, so a Gmail account will
eventually fail with:

```
google.auth.exceptions.RefreshError: ('invalid_grant: Bad Request', ...)
```

The daemon stays up and retries with backoff, but cannot sync until you
re-authorize:

```bash
cd ~/src/localmail
unset VIRTUAL_ENV && ./.venv/bin/localmail oauth-login horst-gmail   # opens a browser
```

The new refresh token lands in the Keychain; the daemon reads it fresh on its
next reconnect (≤ 60 s) — **no restart required**. To reconnect immediately:
`launchctl kickstart -k gui/$(id -u)/com.localmail.daemon`. Expect to repeat
this roughly weekly while the app remains in Testing mode.

## Managing the agents

```bash
UID_NUM=$(id -u)

# Restart after a code update or config change
launchctl kickstart -k gui/$UID_NUM/com.localmail.serve
launchctl kickstart -k gui/$UID_NUM/com.localmail.daemon

# Stop / unload (does not survive next login unless re-bootstrapped)
launchctl bootout gui/$UID_NUM/com.localmail.serve
launchctl bootout gui/$UID_NUM/com.localmail.daemon

# Read-only daemon status via the DB (works even with supervise_daemon=false)
./.venv/bin/localmail daemon status
```

**After pulling new code:** `git pull && uv sync --extra mcp && ./.venv/bin/localmail init-db`,
then `kickstart -k` both agents.

## Optional: create an admin user

The browser admin UI (`/admin`) requires an **admin** account; the `/v1` API and
MCP work with any granted user via a bearer token. To create one:

```bash
./.venv/bin/localmail add-api-user horst --admin     # prompts for a password
./.venv/bin/localmail grant-account horst horst-gmail  # ACL: which accounts they can read
```

Then log in at `https://127.0.0.1:8443/admin/login`.

## Uninstall

```bash
UID_NUM=$(id -u)
launchctl bootout gui/$UID_NUM/com.localmail.daemon
launchctl bootout gui/$UID_NUM/com.localmail.serve
rm ~/Library/LaunchAgents/com.localmail.{daemon,serve}.plist
```

The database, attachments, and Keychain secrets are untouched by removing the
agents.
