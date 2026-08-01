# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-01 02:15 UTC (session 9).** Shipped the **OAuth/keyring
> correctness cluster** (#236, #219, #217) plus both open Dependabot alerts on
> branch `fix/oauth-auth-correctness`, pushed as
> **[PR #240](https://github.com/hherb/localmail/pull/240)**. Also applied
> migration `0033` to **both** live deployments and recovered the DGX from two
> unrelated outages it was already in.
> **Next step: confirm CI green and merge #240 — see §0.**

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

### 1. Deployment catch-up (both hosts)

Last session's `0033_transient_fetches` had **not** been applied anywhere —
`sync_mailbox` reads `transient_fetches` on every mailbox pass, so both
deployments were one daemon restart away from erroring. Applied via
`localmail init-db` on the Mac (port 5532) and on the DGX (which was also 3
commits behind on `main`; pulled + `uv sync --extra mcp` first).

**The DGX was in two unrelated broken states, both now fixed:**

1. **fastembed cache in `/tmp`.** The box rebooted at 07:55 local, `/tmp` was
   cleared, and the daemon re-downloaded ~1 GB of model on every start. A
   restart landing mid-download left the HF snapshot symlink pointing at a
   blob that did not exist, so every subsequent start died with
   `onnxruntime … NO_SUCHFILE: … model.onnx failed` — with `Restart=always`,
   a loop that never converges (5 restarts in ~2 min). Fixed by setting
   `[search] fastembed_cache_dir = "/home/hherb/.cache/fastembed"` in the DGX
   config and pre-downloading the model there (1.2 G, warm-up returns dim 768).
   **Now documented in README's "Embedding model" section** — the default is a
   trap for any always-on Linux deployment, not just this one.
2. **Locked gnome-keyring after the reboot** (the recurring
   [[dgx-keyring-lock-hang]] failure). Needs the login password, so the
   operator ran the unlock. Verified afterwards: `Locked → b false`, all five
   workers `idle` with fresh heartbeats and no `last_error_msg`, and new mail
   ingested at `2026-07-31 23:55 UTC`.

   **One thing genuinely improved since that memory note was written:** this
   failure is **no longer silent**. `daemon_heartbeats` now carries `idle`/`poll`
   rows in state `reconnecting` with `"Failed to unlock the collection!"` in
   `last_error_msg`, rather than the workers parking in D-Bus with no rows at
   all. The admin daemon panel shows it. The memory note's "diagnostic
   signature" (rows *absent*) is therefore out of date.

### 2. OAuth/keyring correctness cluster (branch `fix/oauth-auth-correctness`, PR #240)

Built TDD throughout — **every test was watched failing against the unfixed
code before the fix went in.**

| SHA | what |
|---|---|
| `97f52f4` | the three fixes + pure `src/localmail/account_names.py` + CLAUDE.md |
| `e5733c1` | quinn-proto + postcss bumps, and the carried `--all-targets` clippy fix |
| `e503e47` | README: account-name rule, true revocation scope, persistent embed cache |

**#236 (Low) — an authorization code outlived revocation.** `codes.load_code`
had no `api_users` JOIN, so it honoured a code regardless of `disabled_at` or
`sessions_invalidated_at`. Exchanging it minted an access + refresh pair stamped
`created_at = now()` — past the cutoff, hence valid — handing back exactly the
credentials the operator had just cut off. Now mirrors `refresh.load_refresh`.
**Session revocation covers four credential kinds, not three** (CLAUDE.md
updated). Window was only ~60 s and codes are PKCE-bound, hence Low; but the
`disabled_at` half was the older gap — `load_refresh` has mirrored
`verify_token` on it since the M1 hardening (#182).

**#219 (Medium) — single-use violated by a *failed* exchange.** The
`consume_code` DELETE shared a transaction with the mint, so every post-consume
failure — the disabled-user branch's explicit rollback, or psycopg's
rollback-on-exception from `mint_refresh`/`mint_access`/`touch_last_used` —
rolled the DELETE back and **resurrected the code** for the rest of its TTL
(RFC 6749 §4.1.2). The burn now commits on its own before anything can fail.
Concurrency semantics unchanged: a second exchange's DELETE blocks on the row
lock, matches 0 rows, raises `invalid_grant`.

**#217 (Medium) — an account name could clobber another account's refresh
token.** Names are keyring usernames and the refresh token lives under
`<name>:refresh`, so a password account named literally `gmail:refresh` sent
`store_password` straight over the `gmail` account's OAuth token. New pure
module [src/localmail/account_names.py](src/localmail/account_names.py) owns the
rule (blank / length / separator); **both** create boundaries delegate to it —
`api.admin.accounts._validate_create_fields` and the `config.AccountConfig`
field validator behind the `init-db` TOML seed. Names are not editable after
creation (`_UPDATABLE` has no `name`), so create is the whole surface.

**Design calls worth knowing:**

1. **Rejected the issue's "ideally an allowlist" suggestion.** A conservative
   character allowlist would retroactively break an existing config on a
   re-seed (names with spaces, `+`, unicode). `:` is the *only* character that
   creates keyring ambiguity, so that is the whole rule. Verified neither live
   deployment has an affected name before shipping.
2. **`account_name_error` returns a message rather than raising**, so each
   caller wraps it in its own error type (`AccountFieldError` service-side,
   `ValueError` in the pydantic validator) and the admin UI can render it
   beside the offending input. That rendering is pinned by a test
   (`test_colon_name_rejection_renders_beside_the_name_field`) because
   `_FIELD_HINTS` matches on substrings **in order** — a rewording could
   silently demote the message to a form-level error.
3. **#219's trade is deliberate and stated in the code**: a post-burn failure
   now costs the user a fresh consent round trip. That is strictly cheaper than
   leaving a replayable code.

### 3. Dependabot: both open high alerts cleared

- **`quinn-proto`** 0.11.14 → **0.11.16** (alert #56, needs ≥ 0.11.15) — remote
  memory exhaustion via unbounded out-of-order stream reassembly, reached
  through the Tauri stack.
- **`postcss`** 8.5.15 → **8.5.25** (alert #55, needs ≥ 8.5.18) — path traversal
  in source-map auto-loading, reached through the Svelte/Vite toolchain.

**#90 did not gate either** — its `glib` alert (#3) is **dismissed as
`not_used`**, so no broader Tauri stack bump was needed first. #90 is arguably
closeable on that basis; left open pending your call.

Also cleared the long-carried **`clippy::approx_constant`** failure in
`commands/search.rs`'s test module, so `cargo clippy --all-targets -- -D
warnings` now passes locally. CLAUDE.md's note was rewritten to keep the part
that still matters: CI runs clippy **without** `--all-targets`, so a lint
regression inside a `#[cfg(test)]` module still will not turn `main` red.

**Verification (all run this session):**
- `uv run pytest` → **1869 passed** (`test_daemon_control_socket.py` deselected —
  known macOS `AF_UNIX path too long`)
- `uv run mypy src/localmail` → clean, 125 source files
- `ruff check` → clean on every touched file (repo-wide pre-existing warnings
  unchanged; ruff is not configured in `pyproject.toml` and not in CI)
- `npm run check` (321 files, 0 errors), `npm test` (388 passed), `npm run build`
- `cargo test` (104 passed), `cargo clippy --locked` **and** `--all-targets` clean

## What's next

### 0. **Merge PR #240**
   Pushed with CI running at handoff time. All four jobs gate it (Python +
   `gui/` files both changed).
```bash
gh pr checks 240 --watch && gh pr merge 240 --squash --delete-branch
```
   **No migration this time** — nothing to apply to the deployments afterwards.
   A `git pull` + service restart on each host picks the fixes up whenever
   convenient; none of them is urgent enough to justify an unplanned restart.

### 1. **#237 — orphaned blob temp files accumulate after a hard kill** *(carried)*
   Directly caused by the #231 fix (per-writer `<sha>.<pid>.<uuid>.tmp` names).
   The old shared name was accidentally self-limiting; now a SIGKILL/OOM/power
   loss between write and `replace()` strands a temp nothing collects.
   **Acceptance:** a sweep that removes `*.tmp` files under
   `<attachments.root>/blobs/` older than a configurable age, run somewhere
   sensible (daemon startup and/or a periodic worker tick), with the age as a
   `[attachments]` or `[daemon]` knob rather than a literal. Must not delete a
   temp an active writer is mid-`replace()` on — age-gating is what buys that,
   so pick the default generously and say why in the config comment.

### 2. **#234 — make `Searcher.search`'s `allowed_account_ids` a required keyword** *(carried)*
   Small and high-value: the ACL clamp shipped in #229 defaults to `None`
   ("no ACL"), so a new caller that forgets the kwarg silently gets **full
   cross-account access** rather than a `TypeError`.
   **Acceptance:** the parameter is keyword-only with no default; every
   in-repo caller passes it explicitly (CLI/local callers pass `None` to keep
   full DSL power); tests cover that omitting it raises.

### 3. **#239 — an unfetchable body leaves no queryable record** *(filed last session)*
   Giving up on a held UID logs a WARNING and advances, with no tombstone and
   no `list-failed-fetches` / `retry-failed-fetches`. Every sibling failure path
   in this codebase keeps a re-drivable row; this one doesn't. Not urgent —
   pre-#238 the message was *always* silently lost — but it is the one gap left
   in the ingestion recovery story.

### 4. **Remaining auth/robustness issues** *(the ones this session did not take)*
   - **#220** — `/oauth/consent` login rate-limit ignores `X-Forwarded-For` /
     trusted proxies, unlike `/v1/auth/login` and the DCR guard (M3). Same pure
     `resolve_client_ip` helper applies; likely a small, well-shaped fix.
   - **#221** — daemon supervisor lifecycle robustness (grace mismatch,
     event-loop block/orphan, STARTING-stuck, socket-timeout, chmod TOCTOU).
   - **#216** — extraction extension-allowlist is dead code; MIME-mistyped
     attachments are silently unindexed.
   - **#218** — GUI download commands buffer the full body before enforcing the
     size ceiling.

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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 5).

### 6. **Close #90?**
   Its premise (the `glib` Dependabot alert) is dismissed as `not_used` and no
   longer appears among open alerts. Either close it, or repurpose it explicitly
   as "bump the Tauri stack for its own sake" with a real acceptance criterion.

## Open decisions & risks

1. **DGX post-reboot ritual, now two steps.** Every reboot (a) locks the
   gnome-keyring `login` collection and (b) *used to* clear the fastembed cache.
   (b) is fixed permanently by the persistent `fastembed_cache_dir`; (a) still
   needs the operator's password after each boot:
   ```bash
   printf '%s' "$PASSWORD" | gnome-keyring-daemon --replace --unlock --components=secrets
   systemctl --user restart localmail-daemon
   ```
   Verify with `busctl --user get-property org.freedesktop.secrets
   /org/freedesktop/secrets/collection/login org.freedesktop.Secret.Collection
   Locked` → must read `b false`. **An empty `$PASSWORD` fails silently** — that
   happened once this session; the daemon kept reporting `KeyringLocked` while
   the shell looked successful. PAM auto-unlock would retire this entirely.
2. **#217 is prospective only.** It guards *creation*. If a colliding pair ever
   existed, the OAuth refresh token is already overwritten and unrecoverable —
   re-run `oauth-login` for the affected account. Neither deployment is
   affected (verified: no account name contains `:`).
3. **#219 changes user-visible failure behaviour.** A failed exchange now burns
   the code, so a client that retries the *same* code gets `invalid_grant`
   rather than succeeding on the retry. That is the RFC-correct outcome and the
   whole point of the fix, but if a flaky MCP client starts reporting "consent
   loop" symptoms, this is the change to look at first.
4. **Admin bearer blast radius** *(carried, #204)*: a token issued to an
   `is_admin` user is an admin credential — no per-token scope.
5. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
   stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or vitest
   leaks an unhandled rejection while still printing "passed".
6. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally (`AF_UNIX path too long`); deselect it, Linux CI is the real signal.
   Also carried: psycopg_pool teardown `ResourceWarning`s, the websockets
   `DeprecationWarning` (#25), Starlette TestClient `httpx` `DeprecationWarning`,
   and jsdom `HTMLCanvasElement.getContext` noise in the gui vitest run.
7. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
   slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs. README
   **was** updated this session.
8. **Run vitest from `gui/`, not the repo root** *(carried)* — from the root it
   silently runs without gui's vite config and fails every `.svelte` import with
   a confusing parse error.
9. **`cargo clippy --all-targets` is clean but ungated.** CI lints without
   `--all-targets`, so test-module regressions still won't turn `main` red. Run
   it locally when touching Rust tests.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean
git branch --show-current                # fix/oauth-auth-correctness
gh pr list --state open                  # expect #240 until §0 is done
gh pr checks 240

# §0 — merge; nothing to migrate afterwards this time:
gh pr merge 240 --squash --delete-branch
git checkout main && git pull --ff-only

# Python test suite (deselect the macOS-only socket failure — see risk 6):
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 1869 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 125 source files

# Frontend (MUST be run from gui/ — see risk 8):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..

# DGX health check (after any reboot — see risk 1):
ssh hherb@10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve'
```

`origin/main` at `aff691c`; branch `fix/oauth-auth-correctness` =
`97f52f4` + `e5733c1` + `e503e47`, pushed as PR #240. Latest migration
**`0033_transient_fetches.sql`** (applied on both deployments); next free slot
`0034_*.sql`. Open issues: 21, dropping to 18 when #240 merges (#236, #219,
#217 close with it). Dependabot: **0 open alerts** on the default branch once
#240 lands.
