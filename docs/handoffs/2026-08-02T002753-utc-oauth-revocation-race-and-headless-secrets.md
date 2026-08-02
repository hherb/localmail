# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-02 (session 11).** Migration `0034` is **applied to
> both deployments** and PR #243 merged as `9b61f62`. This session closed the
> OAuth revocation race **#241** and the consent rate-limit gap **#220**, and
> — on the user's request mid-session — built a **reboot-safe headless secret
> store** so the DGX daemon survives a reboot with no operator intervention.
> Branch `fix/oauth-revocation-race-and-headless-secrets` = `a34b6a9`, pushed as
> **[PR #244](https://github.com/hherb/localmail/pull/244)** (CI green).
> **The DGX migration is done and the daemon is healthy on the file backend.**
> **Next step: merge #244, reboot the DGX to prove the fix, put it back on
> `main` — see §0.**

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control. Hybrid search (Phases 1+2) + an HTTPS GUI server + a
remote MCP server (optionally a full OAuth 2.1 authorization server) + the
opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5 GUI
lives under `gui/` — read-only viewer plus an admin mode (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Licensed AGPL-3.0-or-later
(per-file SPDX headers in `src/localmail/`; **not** in `gui/`).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

`origin/main` was at `9b61f62` (PR #243, merged before this session started).

### Ops, completed

Migration `0034_transient_fetches_gave_up.sql` applied to **both** hosts and
both services restarted:

- **Mac** — `init-db` applied `0034`; `com.localmail.daemon` + `com.localmail.serve`
  kickstarted; `localmail list-failed-fetches` → *"no given-up fetches"*.
- **DGX** — pulled, `uv sync --extra mcp`, `init-db` applied `0034`, services
  restarted and `active`. **But the daemon was already crash-looping** on the
  locked gnome-keyring collection: 372 `KeyringLocked` failures in the six hours
  since a reboot, i.e. pre-existing and not caused by the restart. That
  observation is what prompted the third workstream below.

### 1. #241 — session revocation was enforced only at code-load time

The MCP SDK drives `load_authorization_code` and `exchange_authorization_code`
as two separate calls, so the load's verdict is already stale by exchange time.
A *disabled* user was caught indirectly (the minted refresh reads back absent
through `load_refresh`), but `sessions_invalidated_at` was **not**: the
successor carries `created_at = now()`, past the cutoff, so the exchange
completed and returned exactly the credentials the operator had cut off.

- `codes.consume_code` now burns the code **and** re-decides validity in one CTE
  under a single snapshot → `ConsumeResult(burned, user_valid)`. A vanished user
  row yields `user_valid=False` via the LEFT JOIN's NULL: fail closed.
- **Burning stays unconditional.** The issue text proposed a `DELETE … USING
  api_users` guarded on the user, which would leave a revoked user's code
  *unburned* and replayable for its remaining TTL — breaking the #219 single-use
  invariant. Single-use and user-validity are separate concerns.
- **Rotation** already re-ran `load_refresh` at the top of the exchange leg, so
  the SDK's stale load was re-decided — but the claim UPDATE and the mint are
  separate statements with separate READ COMMITTED snapshots. The claim now
  carries the predicate as an `EXISTS`, and a failed claim is **disambiguated**
  by re-reading `_raw_state`: consumed → `reuse`, otherwise → `unknown`.
  Conflating them would delete a token family the operator never targeted.
- New pure [src/localmail/api/revocation_sql.py](src/localmail/api/revocation_sql.py)`::credential_valid_sql`
  is now the single authority, shared by all five sites (`verify_token`,
  `load_refresh`, `load_code`, `consume_code`, the rotation claim).

### 2. #220 — `/oauth/consent` ignored `X-Forwarded-For`

The one half of "reuses the `/v1/auth/login` rate-limit path" never wired up.
Behind a configured proxy every user shared one per-IP bucket. Now peels XFF via
the shared `api.client_ip.resolve_client_ip`.

### 3. Reboot-safe headless secrets *(added mid-session at the user's request)*

Design: [docs/superpowers/specs/2026-08-02-headless-secrets-design.md](docs/superpowers/specs/2026-08-02-headless-secrets-design.md).

The OS keyring **cannot** serve a lingering systemd *user* service: it starts at
boot with no PAM session, and the gnome-keyring `login` collection is unlocked by
PAM at interactive login and by nothing else. Not a misconfiguration — it is what
a login keyring is, and no keyring setting fixes it.

- `[secrets] backend = "keyring" | "file"` (default `keyring`, so **macOS is
  untouched**) + `file_path`.
- Pure [secrets_store.py](src/localmail/secrets_store.py) (username scheme, JSON,
  `mode_is_private`), IO [secrets_file.py](src/localmail/secrets_file.py)
  (`FileSecretStore`), dispatcher `secrets.py`, pure planner
  [secrets_migrate.py](src/localmail/secrets_migrate.py).
- `localmail migrate-secrets [--dry-run]` — always reads the *keyring*, writes the
  *file*, ignores the configured backend (the realistic order is flip → fail →
  migrate), never deletes from the source.
- **Permissions enforced on read**: any group/other bit raises
  `InsecureSecretsFile` naming the `chmod`. Refusing beats warning or
  self-healing — see risk 3.

**Verification (all run this session):**
- `uv run pytest --deselect tests/test_daemon_control_socket.py` →
  **2008 passed** (was 1911; **+97**)
- `uv run mypy src/localmail` → clean, **130** source files (was 126)
- `ruff check` → clean on every touched file
- **No `gui/` changes**, so the Rust/Svelte jobs are unaffected and were not run.
- **No migration** in this PR.

Built TDD throughout — every test was watched failing against the unfixed code
first, including the two that reproduce the #241 leak and the one that
reproduces #220's shared per-IP bucket.

## What's next

### 0. **Merge #244, reboot the DGX to prove the fix, restore `main`**

   **The DGX migration ran and succeeded** (`~/finish-headless-secrets.sh`, with
   the operator's password). Verified state:
   `~/.config/localmail/secrets.json` is `-rw-------` holding
   `horst-gmail:refresh`; `[secrets] backend = "file"` is in `config.toml` (a
   timestamped backup sits beside it); all five workers — `idle`, `poll`,
   `embed`, `extract`, `reconcile` — heartbeat fresh with `state=idle` and
   `last_error_msg=NULL`, and there are **zero** `KeyringLocked` lines since the
   restart. The healthy `idle`/`poll` workers are the real proof: they only
   reach that state after a successful XOAUTH2 login, and with `backend = "file"`
   the keyring is never consulted.

   > **The script's own success check had a bug and reported a false failure.**
   > It grepped `journalctl --since "1 minute ago"`, a window that spans the
   > *pre-restart* crash-loop, so it fires every time regardless. Fixed to scope
   > the window to `systemctl show -p ActiveEnterTimestamp`. If you re-run the
   > staged copy it is the corrected one.

```bash
gh pr checks 244 --watch && gh pr merge 244 --squash --delete-branch
git checkout main && git pull --ff-only
```
   **Still outstanding — the reboot.** A restart is not the test: the keyring is
   currently *unlocked* (the migration unlocked it), so only a cold boot proves
   the daemon no longer depends on it. **Until that has happened, the fix is
   unproven on the host it was written for.**
```bash
ssh hherb@10.0.0.3 'sudo reboot'
# once it is back:
ssh hherb@10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve
  journalctl --user -u localmail-daemon -b | grep -c KeyringLocked'   # expect 0
```
   **Then put the DGX back on `main`** — it is on the feature branch:
```bash
ssh hherb@10.0.0.3 'cd ~/src/localmail && git checkout main && git pull --ff-only \
  && uv sync --extra mcp && systemctl --user restart localmail-daemon localmail-serve'
```

### 1. **#216 — extraction extension-allowlist is dead code** *(carried)*
   MIME-mistyped attachments are silently unindexed. Worth taking next because
   "silently unindexed" is the same class of gap #239 closed for ingestion.
   **Acceptance:** an attachment whose declared MIME is wrong but whose extension
   is recognised (or vice versa) is extracted, with a test that pins the
   classification decision as a pure function.

### 2. **Remaining robustness issues** *(carried)*
   - **#221** — daemon supervisor lifecycle robustness (grace mismatch,
     event-loop block/orphan, STARTING-stuck, socket-timeout, chmod TOCTOU).
   - **#218** — GUI download commands buffer the full body before enforcing the
     size ceiling.
   - **#235** — `search --smart` reports "could not reach the rewriter service"
     forever on a malformed `rewriter_base_url`.
   - **#226** — self-signed cert misses the reachable IP when `--bind 0.0.0.0`.
   - **#225 / #227** — `/v1/changes` subscription lifecycle gaps.
   - **#200 / #211** — admin panels silently swallow 4xx; surface as a toast.

### 3. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
   `/v1/admin/users` is already `require_admin()` (bearer-capable) — **no backend
   work needed.** Service layer:
   [src/localmail/api/admin/users.py](src/localmail/api/admin/users.py).
   **Acceptance:** a `UsersPanel.svelte` replacing the placeholder tab: list,
   create, delete, per-account ACL grant/revoke (a checklist over every account),
   `is_admin` toggle, password reset, enable/disable. Surface the **two lock-out
   guards as 409s** — the count-based last-admin rule (`LastAdminError`) and the
   identity-based self-action rule (no self-demote/self-delete). Mirror
   [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py).
   Follow the Daemon-panel shape, and **stub the new API module in both
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 8).

### 4. **Close #90?** *(carried, still unanswered)*
   Its premise (the `glib` Dependabot alert) is dismissed as `not_used` and no
   longer appears among open alerts. Either close it, or repurpose it explicitly
   as "bump the Tauri stack for its own sake" with a real acceptance criterion.

## Open decisions & risks

1. **The DGX is on a feature branch right now.** It was checked out to run the
   migration; put it back on `main` after #244 merges (§0). If #244 is *not*
   merged, that host silently drifts from `main`.
2. **The headless-secrets fix is unproven until a reboot.** The DGX migration
   ran and the daemon is healthy on the file backend — but the keyring is
   *currently unlocked*, so a restart proves nothing the reboot doesn't. The
   failure this targets only manifests at cold boot. Do not close the loop on
   it without the reboot check in §0.
3. **`InsecureSecretsFile` refuses rather than warns — deliberate.** The daemon
   will crash-loop on a group/other-readable secrets file, which is the same
   *symptom* this whole feature cures. It is the right call for a genuinely
   different problem: the error names the `chmod`, and warning-and-reading would
   leave a leaked credential file unnoticed in a log. Self-healing with a `chmod`
   was rejected too — it rewrites permissions the operator may have set on
   purpose and cannot undo the exposure. Do not "soften" this without revisiting
   the design doc.
4. **`config.load_config()` calls `secrets.configure()` — a deliberate side
   effect.** `load_config` is the only place that sees the resolved config
   including `--config PATH`, and every process that touches a secret loads
   config first. The alternative was threading a store through `open_connection`
   → `sync` → `idle`/`poller` → `Daemon` and the whole admin layer. An autouse
   conftest fixture calls `secrets.reset_to_default()` after every test so a
   config-loading test cannot leak its backend into the next one — **keep that
   fixture** if you add config-loading tests.
5. **#241's unconditional burn is load-bearing.** If a future reviewer proposes
   the issue's original `DELETE … USING api_users` shape, read the
   `consume_code` docstring first: it would leave a revoked user's code
   replayable for its remaining TTL and break `test_failed_exchange_still_burns_the_code`.
6. **#239's manual tombstone retention is still a deliberate call** *(carried)*.
   If an issue asks for an automatic sweep of `gave_up_at` rows, the trade is
   silently deleting the only record of permanently lost mail.
7. **Admin bearer blast radius** *(carried, #204)*: a token issued to an
   `is_admin` user is an admin credential — no per-token scope.
8. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
   stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or vitest
   leaks an unhandled rejection while still printing "passed".
9. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally (`AF_UNIX path too long`); deselect it, Linux CI is the real signal.
   Also carried: psycopg_pool teardown `ResourceWarning`s, the websockets
   `DeprecationWarning` (#25), Starlette TestClient `httpx` `DeprecationWarning`,
   and jsdom `HTMLCanvasElement.getContext` noise in the gui vitest run.
10. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs.
    README **was** updated this session (new "Headless secret storage" section,
    CLI table row, Layout bullet).
11. **Run vitest from `gui/`, not the repo root** *(carried)* — from the root it
    silently runs without gui's vite config and fails every `.svelte` import with
    a confusing parse error.
12. **`cargo clippy --all-targets` is clean but ungated** *(carried)*. CI lints
    without `--all-targets`, so test-module regressions still won't turn `main`
    red. Run it locally when touching Rust tests.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean
git branch --show-current                # fix/oauth-revocation-race-and-headless-secrets
gh pr checks 244

# §0 — merge, then finish + PROVE the DGX fix:
gh pr merge 244 --squash --delete-branch
git checkout main && git pull --ff-only
ssh hherb@10.0.0.3 './finish-headless-secrets.sh'      # needs your password
ssh hherb@10.0.0.3 'cd ~/src/localmail && git checkout main && git pull --ff-only'
# then reboot the DGX and confirm zero KeyringLocked lines

# Python test suite (deselect the macOS-only socket failure — see risk 9):
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 2008 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 130 source files

# Frontend — only if you touch gui/ (MUST be run from gui/ — see risk 11):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..

# Smoke-test the new ops surface against the live archive:
unset VIRTUAL_ENV && uv run localmail migrate-secrets --dry-run   # Mac: reports, changes nothing

# DGX health check (after any reboot):
ssh hherb@10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve'
```

`origin/main` at `9b61f62`; branch
`fix/oauth-revocation-race-and-headless-secrets` = `a34b6a9`, pushed as PR #244.
Latest migration **`0034_transient_fetches_gave_up.sql`** (**applied to both
deployments this session**); next free slot `0035_*.sql`. Open issues: 16,
dropping to 14 when #244 merges (#241, #220 close with it). Dependabot: 0 open
alerts.
