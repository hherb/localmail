# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-02 (session 10).** Cleared the **carried
> ingestion/robustness trio** — #239 (give-up tombstones + two new CLI
> commands), #237 (blob-temp sweeper), #234 (required ACL kwarg) — on branch
> `fix/ingestion-recovery-and-acl-kwarg`, pushed as
> **[PR #243](https://github.com/hherb/localmail/pull/243)**.
> **Next step: confirm CI green, merge #243, then apply migration `0034` to both
> deployments *before* restarting their daemons — see §0.**

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

Session 9's PR #240 merged as **`c9c011e`**; a docs runbook landed after the
last handoff was written as **`be646a2`** (PR #242). `origin/main` was at
`be646a2` when this session started. **New issue filed since:** #241 (the
`load_authorization_code` → `exchange_authorization_code` revocation race —
already documented in CLAUDE.md, not fixed).

### The carried trio (branch `fix/ingestion-recovery-and-acl-kwarg`, PR #243)

One commit, **`44b50fb`**. Built TDD throughout — every test was watched
failing against the unfixed code before the fix went in.

**#239 (Low-Medium) — giving up on an unfetchable `BODY[]` left no queryable
record.** Past `[daemon] max_body_fetch_hold_s`, sync advanced the watermark,
logged one WARNING, and `reclaim_below` collected the row at the next
checkpoint. The message was then permanently absent, invisible to every report,
and unreachable by every retry command — the *only* failure path in this
codebase without a re-drivable row.

- **Migration `0034_transient_fetches_gave_up.sql`** adds
  `transient_fetches.gave_up_at` + a partial index.
- `fetch_retry.mark_gave_up` stamps it via `COALESCE(gave_up_at, now())` —
  **never restamping**, so it keeps meaning "since when has this been missing"
  across the re-sightings a lower held UID causes. `first_seen_at` is untouched,
  so expiry stays sticky.
- `reclaim_below` now skips `gave_up_at IS NOT NULL`. It still collects live
  holds orphaned above the old watermark (a held UID later expunged drops out of
  SEARCH and is never re-seen) — that path is still load-bearing, not dead code.
- **`localmail list-failed-fetches` / `retry-failed-fetches`.** Retry rewinds
  each affected mailbox's `uidnext` to its *lowest* tombstoned UID via the pure
  `plan_uidnext_rewind`, then purges.

**#237 (Low) — orphaned blob temps after a hard kill.** New pure module
[src/localmail/blob_temps.py](src/localmail/blob_temps.py) owns **both** the
minting (`new_temp_path`, now called by `write_attachments`) and the matching
(`is_writer_temp`). Swept at `Daemon.start_workers` (best-effort) and via
`localmail sweep-blob-temps [--dry-run]`. Gated on **age**
(`[attachments] temp_max_age_s`, default 24 h), not pid liveness.

**#234 (hardening) — `Searcher.search`'s `allowed_account_ids` defaulted to
`None`.** Now keyword-only with no default; all 30 in-repo call sites state
`None` or a real list explicitly.

**Design calls worth knowing:**

1. **#239's retention is manual, and that is a deliberate departure from the
   issue text.** The issue said "tombstones need their own retention, or they
   become the next unbounded table." A tombstone is written once per distinct
   unfetchable UID and upserted thereafter, so growth is bounded by the number
   of genuinely broken messages — not a runaway. An automatic sweep would trade
   that negligible growth for silently deleting the sole record of permanently
   lost mail, which is the failure #239 exists to end. `failed_messages` and
   `failed_extractions` make the same call. The mechanism exists
   (`retry-failed-fetches --forget [--older-than-days N]`); it is just not
   automatic. **Do not add a background expiry without revisiting this.**
2. **`blob_temps` owns minting *and* matching in one module on purpose.** The
   temp-name format is a coupling between `attachments.py` and the sweeper; if
   they lived apart, renaming the format would silently strand every future
   orphan with no test failure. Pinned by a test that simulates SIGKILL — a
   `replace` that *never happens*, not a raised exception, because
   `write_attachments`'s own `except BaseException: unlink` already covers the
   raise and would have tested the wrong thing.
3. **The sweeper's name match is strict** (`<64 hex>.<digits>.<32 hex>.tmp`),
   never a `*.tmp` glob. It deletes without asking, and the blob tree is a
   directory operators poke at.
4. **Two `test_sync.py` assertions were updated, not deleted.** They encoded the
   old "giving up must not leave the row behind" contract. They now assert the
   new one: the tombstone survives, no **live** hold does.

**Verification (all run this session):**
- `uv run pytest --deselect tests/test_daemon_control_socket.py` →
  **1911 passed** (was 1869; +42 new)
- `uv run mypy src/localmail` → clean, **126** source files
- `ruff check` → clean on every touched file (repo-wide pre-existing warnings in
  `searcher.py` / `search/__init__.py` unchanged; ruff is not in CI)
- **No `gui/` changes**, so the Rust/Svelte jobs are unaffected and were not
  re-run this session.

## What's next

### 0. **Merge PR #243, then migrate both deployments**
   CI was still running at handoff time. Only the pytest job gates it (no
   `gui/` files changed).
```bash
gh pr checks 243 --watch && gh pr merge 243 --squash --delete-branch
```
   **This one HAS a migration.** `sync_mailbox` calls `mark_gave_up` on the
   give-up branch, which needs `transient_fetches.gave_up_at`. Apply **before**
   restarting the daemon on each host, or a give-up will raise:
```bash
# Mac (port 5532)
cd /Users/hherb/src/localmail && git pull --ff-only && uv run localmail init-db
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon   # confirm label first

# DGX
ssh hherb@10.0.0.3 'cd ~/src/localmail && git pull --ff-only \
  && uv sync --extra mcp && uv run localmail init-db \
  && systemctl --user restart localmail-daemon localmail-serve'
```
   The `0034` `ALTER TABLE … ADD COLUMN` is a metadata-only change (nullable, no
   default), so it takes a brief ACCESS EXCLUSIVE lock and returns immediately
   even on a large `transient_fetches`. No `estimate-upgrade` pre-flight needed.

### 1. **#241 — session revocation is not enforced between code load and exchange** *(new, filed 2026-08-01)*
   Direct follow-up to last session's #236. The SDK calls
   `load_authorization_code` then `exchange_authorization_code`, and
   `_exchange_code_sync` re-checks nothing before `consume_code`. A *disabled*
   user is caught indirectly (the minted refresh reads back absent through
   `load_refresh` → `user_vanished`), but `sessions_invalidated_at` is **not**:
   the successor refresh carries `created_at = now()`, past the cutoff. A
   revocation landing in that window still yields a token pair. The same race
   exists on the refresh-rotation path.
   **Acceptance:** the exchange leg re-checks the user's `disabled_at` +
   `sessions_invalidated_at` inside the same transaction as `consume_code` (or
   the mint is made conditional on them), for **both** the code and rotation
   paths; a test drives a revocation between the two calls and asserts
   `invalid_grant`. Note #219 split the burn's commit from the mint — check that
   interaction carefully rather than re-merging the transactions.

### 2. **#220 — `/oauth/consent` login rate-limit ignores `X-Forwarded-For`** *(carried)*
   Unlike `/v1/auth/login` and the DCR guard (M3). The same pure
   `api.client_ip.resolve_client_ip` helper applies; likely a small,
   well-shaped fix and a natural pairing with §1.
   **Acceptance:** the consent login limiter peels XFF against
   `auth.trusted_proxies` / `trusted_proxies_max_hops` exactly like the other
   two; empty config = socket peer, unchanged.

### 3. **#216 — extraction extension-allowlist is dead code** *(carried)*
   MIME-mistyped attachments are silently unindexed. Worth a look because
   "silently unindexed" is the same class of gap #239 just closed for ingestion.

### 4. **Remaining robustness issues** *(carried)*
   - **#221** — daemon supervisor lifecycle robustness (grace mismatch,
     event-loop block/orphan, STARTING-stuck, socket-timeout, chmod TOCTOU).
   - **#218** — GUI download commands buffer the full body before enforcing the
     size ceiling.
   - **#235** — `search --smart` reports "could not reach the rewriter service"
     forever on a malformed `rewriter_base_url`.
   - **#226** — self-signed cert misses the reachable IP when `--bind 0.0.0.0`.
   - **#225 / #227** — `/v1/changes` subscription lifecycle gaps.

### 5. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 6).

### 6. **Close #90?** *(carried, still unanswered)*
   Its premise (the `glib` Dependabot alert) is dismissed as `not_used` and no
   longer appears among open alerts. Either close it, or repurpose it explicitly
   as "bump the Tauri stack for its own sake" with a real acceptance criterion.

## Open decisions & risks

1. **#243 needs `init-db` before a daemon restart** — see §0. This is the first
   migration since `0033` and the daemon *writes* to the new column on a
   give-up path, so an un-migrated host will raise there rather than degrade.
2. **#239's manual retention is a deliberate call, not an oversight.** If a
   future reviewer or issue asks for an automatic sweep of `gave_up_at` rows,
   read design call 1 above first — the trade is silently deleting the only
   record of permanently lost mail.
3. **`retry-failed-fetches` triggers a full re-scan above the rewind point.** It
   is idempotent (existing-id check + `ON CONFLICT DO NOTHING`) but not free —
   one slower pass per affected mailbox. The CLI says so; if an operator reports
   "sync suddenly got slow", this is the first thing to check.
4. **DGX post-reboot ritual** *(carried)*: every reboot locks the gnome-keyring
   `login` collection and needs the operator's password:
   ```bash
   printf '%s' "$PASSWORD" | gnome-keyring-daemon --replace --unlock --components=secrets
   systemctl --user restart localmail-daemon
   ```
   Verify with `busctl --user get-property org.freedesktop.secrets
   /org/freedesktop/secrets/collection/login org.freedesktop.Secret.Collection
   Locked` → must read `b false`. **An empty `$PASSWORD` fails silently.** The
   fastembed-cache half of this ritual is permanently fixed
   (`[search] fastembed_cache_dir`). PAM auto-unlock would retire the rest.
5. **Admin bearer blast radius** *(carried, #204)*: a token issued to an
   `is_admin` user is an admin credential — no per-token scope.
6. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
   stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or vitest
   leaks an unhandled rejection while still printing "passed".
7. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally (`AF_UNIX path too long`); deselect it, Linux CI is the real signal.
   Also carried: psycopg_pool teardown `ResourceWarning`s, the websockets
   `DeprecationWarning` (#25), Starlette TestClient `httpx` `DeprecationWarning`,
   and jsdom `HTMLCanvasElement.getContext` noise in the gui vitest run.
8. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
   is a no-op; slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs.
   README **was** updated this session.
9. **Run vitest from `gui/`, not the repo root** *(carried)* — from the root it
   silently runs without gui's vite config and fails every `.svelte` import with
   a confusing parse error.
10. **`cargo clippy --all-targets` is clean but ungated** *(carried)*. CI lints
    without `--all-targets`, so test-module regressions still won't turn `main`
    red. Run it locally when touching Rust tests.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean
git branch --show-current                # fix/ingestion-recovery-and-acl-kwarg
gh pr list --state open                  # expect #243 until §0 is done
gh pr checks 243

# §0 — merge, THEN migrate both hosts before restarting their daemons:
gh pr merge 243 --squash --delete-branch
git checkout main && git pull --ff-only
uv run localmail init-db                 # applies 0034 on the Mac (port 5532)

# Python test suite (deselect the macOS-only socket failure — see risk 7):
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 1911 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 126 source files

# Frontend — only if you touch gui/ (MUST be run from gui/ — see risk 9):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..

# Smoke-test the two new ops surfaces against the live archive:
unset VIRTUAL_ENV && uv run localmail list-failed-fetches
unset VIRTUAL_ENV && uv run localmail sweep-blob-temps --dry-run

# DGX health check (after any reboot — see risk 4):
ssh hherb@10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve'
```

`origin/main` at `be646a2`; branch `fix/ingestion-recovery-and-acl-kwarg` =
`44b50fb`, pushed as PR #243. Latest migration
**`0034_transient_fetches_gave_up.sql`** (**not yet applied to either
deployment**); next free slot `0035_*.sql`. Open issues: 19, dropping to 16 when
#243 merges (#239, #237, #234 close with it). Dependabot: 0 open alerts.
