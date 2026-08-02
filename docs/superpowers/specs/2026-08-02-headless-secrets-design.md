# Reboot-safe headless secret storage

**Status:** design approved 2026-08-02.
**Problem owner:** the DGX deployment, which cannot sync after a reboot without
an operator typing a password.

## The problem

`localmail.secrets` stores IMAP passwords and Gmail OAuth refresh tokens in the
OS keyring: macOS Keychain on darwin, Secret Service (gnome-keyring) on Linux.

On the DGX the sync daemon is a **lingering** systemd *user* service
(`Linger=yes`), so it starts at boot with a session D-Bus but **no PAM login**.
The gnome-keyring `login` collection is unlocked by PAM at interactive login and
by nothing else, so at boot it is locked and stays locked. Every
`keyring.get_password` raises `KeyringLocked`, the daemon dies, `Restart=always`
restarts it, and it dies again — 372 such failures were logged in the six hours
after one reboot. The operator's only recourse is to SSH in and run

```bash
printf '%s' "$PASSWORD" | gnome-keyring-daemon --replace --unlock --components=secrets
```

which is a manual step after every reboot, silently a no-op on an empty
`$PASSWORD`, and impossible to automate without storing the keyring password
somewhere — which is the thing the keyring exists to avoid.

**This is not a bug in keyring and it is not fixable by configuring keyring.**
A collection that requires an interactive unlock cannot serve a process that
starts before any interactive session exists. PAM auto-unlock does not help: the
whole point of lingering is that the service runs with no login.

macOS is unaffected — the launchd agents run inside the user's GUI session, whose
login unlocks the Keychain — and nothing here changes that.

## Non-goals

- Encrypting secrets at rest beyond file permissions. Explicitly decided: the
  disk is already encrypted, and anyone who can read a 0600 file owned by the
  service user is already that user or root. `systemd-creds`/TPM was considered
  and rejected — Linux-only, and `$CREDENTIALS_DIRECTORY` is read-only, so the
  CLI and admin write paths could not use it.
- Changing how macOS stores secrets.
- Any implicit fallback from a failing keyring to the file store. A locked
  keyring must keep raising; silently spilling secrets onto disk because a
  read failed is not a behaviour an operator can reason about.

## Design

### Module layout

`secrets.py` keeps all seven public functions and their signatures — no caller
changes — and becomes a dispatcher over two implementations.

| Module | Kind | Contents |
| --- | --- | --- |
| `secrets_store.py` | **pure** | the `<name>` / `<name>:refresh` username scheme, JSON serialise/deserialise, `mode_is_private(mode)` |
| `secrets_file.py` | IO | `FileSecretStore(path)` — `get` / `set` / `delete` |
| `secrets.py` | IO | `KeyringSecretStore`, the module-level dispatcher, `configure()` |

The username scheme moves *out* of `secrets.py` into the pure module and stays
the single authority for it. Both backends key on the identical strings, which
is what makes them interchangeable and keeps the #217 colon rule meaningful for
both.

### Backend selection

```toml
[secrets]
backend = "keyring"   # or "file"
file_path = "~/.config/localmail/secrets.json"
```

`backend` defaults to `keyring`, so every existing deployment and every existing
test is unchanged. `file_path` expands `~` and environment variables.

`secrets.configure(backend, file_path)` sets the process-wide store, and
**`config.load_config()` calls it**. That is a deliberate side effect, documented
in both modules, chosen because:

- `load_config()` is the only place that knows the *resolved* config, including
  the `--config PATH` override. A lazy self-load inside `secrets` would silently
  ignore that flag.
- Every process that can touch a secret loads config first, by construction.
- The alternative — threading a store object through `open_connection` → `sync`
  → `idle`/`poller` → `Daemon` and through the admin service layer — is a large
  refactor of unrelated code for no functional gain.

`secrets.reset_to_default()` restores the keyring backend; an autouse conftest
fixture calls it after every test so a config-loading test cannot leak its
backend into the next one.

### File format and write discipline

```json
{"version": 1, "secrets": {"<username>": "<value>"}}
```

- The directory is created mode 0700, the file mode 0600.
- Writes go to an `O_CREAT|O_EXCL` temp in the **same directory**, then
  `os.replace`. A reader therefore never sees a partial file, and the secret is
  never briefly present at a wider mode.
- The temp is `fsync`ed before the rename and the **directory** after it.
  `os.replace` buys atomicity against a concurrent reader, not durability: on an
  unclean shutdown an unflushed rename can land pointing at a zero-length file.
  A store that exists to survive reboots cannot take that trade — recovering a
  lost refresh token costs an interactive OAuth consent round trip, on a host
  with no browser.
- `O_EXCL` on a fixed temp name means a write killed where no cleanup handler
  runs (SIGKILL, OOM, power loss) blocks every later write. That surfaces as
  `StaleSecretsTempFile` naming the `rm` to run, rather than a bare
  `FileExistsError` for a dotfile the operator has never seen. The stray temp is
  never clobbered: it may hold the secret from the interrupted write.
- A missing file reads as "no secrets stored" — not an error. That matches an
  empty keyring and keeps `list-accounts` working on a fresh install.
- `delete` of an absent key is a no-op, matching the keyring backend's
  swallowed `PasswordDeleteError`.

### Permission enforcement

On read, if the file's mode has any group or other bits set, raise
`InsecureSecretsFile` naming the path and the exact `chmod 600` to run.

**Refusing rather than warning is deliberate.** The daemon will crash-loop until
it is fixed — the same symptom this whole design exists to cure — but for a
genuinely different reason: a world-readable secrets file is a real exposure, the
diagnosis is in the error message, and the fix is one command. Warning-and-reading
would leave the exposure sitting unnoticed in a log; self-healing with a `chmod`
would rewrite permissions the operator may have set on purpose and cannot undo
the exposure that already happened.

`set` always writes 0600, so the only route to a bad mode is manual tampering,
a restore, or an `rsync` that flattened it.

### Migration

```
localmail migrate-secrets [--dry-run]
```

Copies keyring → file for every account row in the DB, both the password key and
the refresh-token key, skipping the ones the keyring does not hold. Prints a
per-account summary and a total.

One direction only: migrating *away* from the headless store has no use case.
It does **not** delete from the keyring — the operator can, and a failed
migration must be re-runnable.

The operator story is: unlock the DGX keyring one final time, run
`migrate-secrets`, set `backend = "file"`, restart. The unlock ritual is then
retired permanently, and no OAuth consent flow has to be re-driven on a headless
box.

## Failure modes

| Situation | Behaviour |
| --- | --- |
| `backend = "file"`, file absent | reads return `None`; first `set` creates dir + file |
| file mode has group/other bits | `InsecureSecretsFile` on read, with the `chmod` to run |
| file is not valid JSON | raises, naming the path — a corrupt store must not read as empty, which would look like "no secret configured" and send the operator down the wrong path |
| `backend` is an unknown string | pydantic rejects it at config load, before any process starts |
| keyring locked, `backend = "keyring"` | unchanged — `KeyringLocked` propagates |

## Testing

- **Pure** (`secrets_store.py`): username scheme, serialise/deserialise round
  trip, `mode_is_private` across 0600 / 0640 / 0604 / 0666 / 0700.
- **File store** (`tmp_path`): set→get round trip; overwrite; delete; delete of
  an absent key; missing file reads `None`; created file is exactly 0600 and its
  directory 0700; a 0644 file raises `InsecureSecretsFile`; corrupt JSON raises;
  no `*.tmp` remains after a write.
- **Dispatcher**: default is keyring; `configure("file", …)` routes there;
  `reset_to_default()` restores.
- **Migration**: copies both key kinds, skips absent ones, `--dry-run` writes
  nothing.

## Deployment

DGX only. Mac config is untouched.

```bash
# one final unlock
printf '%s' "$PASSWORD" | gnome-keyring-daemon --replace --unlock --components=secrets
uv run localmail migrate-secrets
# add [secrets] backend = "file" to config.toml
systemctl --user restart localmail-daemon localmail-serve
```

Verified by a real reboot: the daemon must reach `active` and sync with no
operator action.
