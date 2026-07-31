# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-07-31 21:07 UTC (session 8).** Cleared two stale green PRs
> off `main`, then shipped the **ingestion correctness cluster** — #215 and both
> halves of #222 — on branch `fix/ingestion-uid-and-message-id`, pushed as
> **[PR #238](https://github.com/hherb/localmail/pull/238)**.
> **Next step: confirm CI green and merge #238 — see §0.**
>
> **Note on the previous handoff:** NEXT_SESSION.md was **four sessions stale** —
> it described session 7 (Daemon panel, PR #207) and its §0 "push + open PR" was
> long done. Sessions that shipped #209–#231 never refreshed it. If you are
> reading a handoff whose §0 looks already-finished, check `git log` and
> `gh pr list` before believing it.

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

### 1. Cleared two stale PRs (reviewed, then merged)

Both had been sitting green and unreviewed since 2026-07-30.

| SHA | what |
|---|---|
| `833fa80` | **#232** — classify Gmail token-refresh failures as connect failures (#158). `google.auth.exceptions.GoogleAuthError` joins `CONNECT_FAILURE_EXC_TYPES`, so a revoked refresh token surfaces as a clean 400 instead of a 500. |
| `9565e88` | **#233** — GUI `AccountForm` dispatches on the edit *intent* (`accountId`), not on whether the row loaded; a failed GET on mount no longer silently **creates a stray account** instead of updating. |

### 2. Ingestion correctness cluster (branch `fix/ingestion-uid-and-message-id`, PR #238)

Design brainstormed and written up first:
[docs/superpowers/specs/2026-08-01-ingestion-uid-and-message-id-design.md](docs/superpowers/specs/2026-08-01-ingestion-uid-and-message-id-design.md).
Built TDD throughout — **and where a fix was already in place, it was
temporarily reverted to confirm the test genuinely caught the bug.** That
discipline paid for itself twice (see "Two things the RED phase caught" below).

| SHA | what |
|---|---|
| `6e059de` | spec |
| `a650baa` | the three fixes + pure `src/localmail/uids.py` |
| `40eea4b` | review follow-ups: bound the hold (migration `0033`), skip redundant probes, correct a false docs claim |
| `358f256` | README: same-stem imports, archive retry re-allocation, empty-body cap |

**#215 (High) — import UID collision poison-pilled healthy messages.** Imports
invent synthetic UIDs from a per-run counter restarting at 0, and mailboxes
resolve on `(account_id, name)` from the source's filename stem — so
`2023/Inbox.mbox` then `2024/Inbox.mbox` into one archive account recycled
committed UIDs and every collision on `message_labels UNIQUE (mailbox_id, uid)`
sent a good message to `failed_messages`. Non-recoverable: `retry-failed`
replayed the same stored uid. Now: continue from `MAX(uid) + 1` per mailbox;
`retry_failed_messages` re-allocates **for archive accounts only** (the recovery
path for already-poisoned rows). Live accounts replay verbatim — a clash there is
a real invariant violation, which is also why `upsert_label` was deliberately
**not** made collision-tolerant.

**#222B — degenerate `Message-Id`.** An empty angle-addr (`<>`) parsed as a
truthy, non-unique string and collapsed distinct messages onto one row.
`parser.normalize_message_id` returns `None` for it; `raw_sha256` dedup engages.

**#222A — empty `BODY[]`.** One `SEARCH UID n:n` probe now separates expunged
(advance) from still-present (hold the resume point via the pure
`checkpoint_uidnext`; next run re-fetches). A failing probe assumes transient.
The hold is **bounded** by `[daemon] max_body_fetch_retries` (default 5,
consecutive) via migration `0033_transient_fetches`.

**Two new pure modules** (project convention — logic out of the 800-line
`sync.py`): [src/localmail/uids.py](src/localmail/uids.py) (UID arithmetic) and
[src/localmail/fetch_retry.py](src/localmail/fetch_retry.py) (bounded-hold
bookkeeping).

**Two things the RED phase caught — worth internalising:**

1. **The first #222B regression test passed with the fix reverted.** It used a
   whitespace-only `Message-Id`, which `email.policy.default` already collapses
   to `""` — so the pre-existing `if message_id` guard caught it. The issue's
   own description ("`Message-Id: <whitespace>`") pointed at the wrong form. Only
   after switching the fixture to `<>` did it fail (`{'INBOX': 1} == {'INBOX': 2}`
   — one message swallowing the other). **A bug report's stated trigger is a
   hypothesis; verify it reproduces before trusting the test.**
2. **The first bounded-hold test encoded the wrong arithmetic** (expected `cap=2`
   to allow two holds). `fetch_budget_exhausted(count, cap) = count >= cap`
   matches `transient_budget_exhausted`, so the cap-th attempt is the one that
   gives up. Fixed the test, not the code.

**Three review findings, all verified against the code before acting:**

1. **The hold was unbounded** — and "still present on the server" is not "will
   ever be fetchable". A **zero-length message** satisfies `if not raw`
   (`raw = b""`) while the probe genuinely finds it, and `idle.py::_sync_inbox`
   runs on **every IDLE notification**, so a stuck low UID re-downloads the whole
   INBOX tail *per new mail*. Unbounded, the fix was worse than the defect. Fixed
   with migration `0033` + config cap (modelled on `transient_extractions`/#153).
2. **Redundant probes.** Once `hold_at` is set the checkpoint is pinned
   (`min(highest_seen+1, hold_at) == hold_at`, UIDs ascend), so the probe cannot
   change the outcome — while whatever empties one `BODY[]` tends to empty the
   whole tail, each probe being a round trip bounded only by `imap_timeout_s`
   against an already-sick server. Now short-circuited.
3. **A load-bearing docs claim was false.** I had written that
   `message_labels.uid` is read by no consumer — the entire safety argument for
   re-allocating it. `sync.backfill_internal_date` **does** read it, as an IMAP
   FETCH key. Re-allocation is safe because of the **archive-account gate**, not
   because nothing reads the column. Corrected in CLAUDE.md and the spec, with an
   explicit warning about what widening that gate would break.

**Verification (all run this session):**
- `uv run pytest` → **1837 passed** (`test_daemon_control_socket.py` deselected —
  known macOS `AF_UNIX path too long`)
- `uv run mypy src/localmail` → clean, 124 source files
- `ruff check` → clean on every touched file (the repo-wide 130 are pre-existing;
  ruff is not configured in `pyproject.toml` and not in CI)

## What's next

### 0. **Merge PR #238**
   Pushed with CI running at handoff time. Only the pytest job gates it (no
   `gui/` files changed).
```bash
gh pr checks 238 --watch && gh pr merge 238 --squash --delete-branch
```
   **After merging, apply migration `0033` to the live archives** — this is the
   first migration in a while and the daemon will not pick it up on its own:
```bash
uv run localmail init-db          # local (port 5532)
# and on the DGX deployment — see the DGX memory note
```

### 1. **#237 — orphaned blob temp files accumulate after a hard kill**
   Directly caused by the #231 fix (per-writer `<sha>.<pid>.<uuid>.tmp` names).
   The old shared name was accidentally self-limiting; now a SIGKILL/OOM/power
   loss between write and `replace()` strands a temp nothing collects.
   **Acceptance:** a sweep that removes `*.tmp` files under
   `<attachments.root>/blobs/` older than a configurable age, run somewhere
   sensible (daemon startup and/or a periodic worker tick), with the age as a
   `[attachments]` or `[daemon]` knob rather than a literal. Must not delete a
   temp an active writer is mid-`replace()` on — age-gating is what buys that,
   so pick the default generously and say why in the config comment.

### 2. **#234 — make `Searcher.search`'s `allowed_account_ids` a required keyword**
   Small and high-value: the ACL clamp shipped in #229 defaults to `None`
   ("no ACL"), so a new caller that forgets the kwarg silently gets **full
   cross-account access** rather than a `TypeError`.
   **Acceptance:** the parameter is keyword-only with no default; every
   in-repo caller passes it explicitly (CLI/local callers pass `None` to keep
   full DSL power); tests cover that omitting it raises.

### 3. **Remaining OAuth/auth correctness cluster** *(the option not taken this session)*
   - **#236** — `oauth.codes.load_code` ignores `disabled_at` and
     `sessions_invalidated_at`, leaving a ~60s post-revocation window in which a
     held authorization code still exchanges. **Acceptance:** the code lookup
     JOINs `api_users` and applies both predicates, mirroring
     `refresh.load_refresh` (the M1 fix); a revoked user's in-flight code fails
     closed with `invalid_grant`.
   - **#219** — authorization-code single-use violated on mid-exchange failure.
   - **#217** — an account name containing `:` collides with the keyring
     username scheme (`<name>:refresh`) and can clobber an OAuth refresh token.
     **Acceptance:** reject `:` in account names at the validation boundary
     (`create_account` + the admin form), with a clear message.

### 4. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
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

### 5. **(Carried) `cargo clippy --all-targets -- -D warnings` fails on `main`**
   `gui/src-tauri/src/commands/search.rs:189` uses `3.14` as a dummy `took_ms`
   → `clippy::approx_constant`. Pre-existing. CI gates clippy **without**
   `--all-targets`, so `#[cfg(test)]` modules are never linted and `main` stays
   green. One-character fix whenever someone is in that file.

## Open decisions & risks

1. **Migration `0033` must be applied to live deployments** before running the
   new code — `sync_mailbox` reads `transient_fetches` on every mailbox pass and
   will error on a DB that has not migrated. Both the macOS and DGX deployments
   need `localmail init-db`.
2. **`max_body_fetch_retries` semantics are "consecutive", and the cap-th attempt
   is the one that gives up** (`count >= cap`, matching
   `transient_budget_exhausted`). Default 5 = four retries then drop. If a real
   transient routinely lasts longer than five syncs, raise it — but note that
   under IDLE a "sync" happens per new mail, so five can elapse in seconds on a
   busy INBOX. **This is the knob to watch in production**; it is the one number
   in this change chosen by analogy rather than measurement.
3. **The #222B fix is prospective only.** Any pair of messages already collapsed
   by a degenerate `Message-Id` cannot be recovered — the second message's bytes
   were never stored. No backfill is possible; don't promise one.
4. **The archive gate is now load-bearing in a way it wasn't before.** Widening
   `should_reallocate_uid` (or the importer) to a live account would make
   `sync.backfill_internal_date` FETCH a synthetic UID against the real server
   and write **another message's** INTERNALDATE onto the row. CLAUDE.md warns
   about this at the definition site.
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
8. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
   slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs. README
   **was** updated this session (all three fixes are user-visible).
9. **Run vitest from `gui/`, not the repo root** *(carried)* — from the root it
   silently runs without gui's vite config and fails every `.svelte` import with
   a confusing parse error.
10. **Keep NEXT_SESSION.md current.** It went four sessions stale, which cost
    this session a full re-orientation pass. The `/nextsession` skill's final
    step is not optional.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean
git branch --show-current                # fix/ingestion-uid-and-message-id
gh pr list --state open                  # expect #238 until §0 is done
gh pr checks 238

# §0 — merge, then apply the migration locally:
gh pr merge 238 --squash --delete-branch
git checkout main && git pull --ff-only
unset VIRTUAL_ENV && uv run localmail init-db

# Python test suite (deselect the macOS-only socket failure — see risk 7):
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 1837 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 124 source files

# Frontend (untouched this session; MUST be run from gui/ — see risk 9):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings && cd ../..
```

`origin/main` at `9565e88`; branch `fix/ingestion-uid-and-message-id` =
`6e059de` + `a650baa` + `40eea4b` + `358f256`, pushed as PR #238. Latest
migration **`0033_transient_fetches.sql`**; next free slot `0034_*.sql`.
Open issues: 22 (was 22 — #215 and #222 close with #238, #234–#237 were filed
before this session). Dependabot open alerts: **0**.
