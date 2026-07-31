# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-07-31 22:10 UTC (session 8).** Cleared two stale green PRs
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
| `40eea4b` | review pass 1: bound the hold (migration `0033`), skip redundant probes, correct a false docs claim |
| `358f256` | README: same-stem imports, archive retry re-allocation, empty-body cap |
| `cce1586` | review pass 2a: `record_attempt`'s SAVEPOINT moves outside the `try` (house shape) + `tests/test_fetch_retry.py` |
| `afe87f4` | review pass 2b: bound the hold by **time**, not attempt count; fix the probe skip; reclaim stale rows |

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
The hold is **bounded by elapsed time** — `[daemon] max_body_fetch_hold_s`
(default 1800) — tracked in `transient_fetches` (migration `0033`).

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

**Two review passes, six findings, all verified against the code before acting:**

*Pass 1 (on the three fixes):*

1. **The hold was unbounded** — and "still present on the server" is not "will
   ever be fetchable". A **zero-length message** satisfies `if not raw`
   (`raw = b""`) while the probe genuinely finds it, and `idle.py::_sync_inbox`
   runs on **every IDLE notification**, so a stuck low UID re-downloads the whole
   INBOX tail *per new mail*. Unbounded, the fix was worse than the defect.
2. **Redundant probes** once the run knows the tail is bad — each is a round trip
   bounded only by `imap_timeout_s` against an already-sick server.
3. **A load-bearing docs claim was false.** I had written that
   `message_labels.uid` is read by no consumer — the entire safety argument for
   re-allocating it. `sync.backfill_internal_date` **does** read it, as an IMAP
   FETCH key. Re-allocation is safe because of the **archive-account gate**, not
   because nothing reads the column. Corrected with an explicit warning about
   what widening that gate would break.

*Pass 2 (on pass 1's own code — all three were defects I introduced):*

4. **The cap counted sync passes, which is the wrong unit.** IDLE fires a pass
   per notification, *including another client toggling a flag*, so five
   unrelated events in ten seconds burned a 5-attempt budget and dropped a
   message over a blip that resolved a minute later — while the poll plane got
   25 minutes from the same number. #153's consecutive-failure cap does **not**
   transfer: its sweeps are timer-paced, so there a count *is* a duration. And a
   count never bounded the re-fetch traffic anyway — that comes from holding the
   watermark, which happens per pass regardless. Now a **duration**
   (`max_body_fetch_hold_s`), anchored on the `first_seen_at` the table already
   had, so no migration change was needed.
5. **The probe skip keyed on `hold_at`**, which is only assigned on the
   still-holding branch — so on the run where every held UID finally expired it
   stayed `None` and the entire tail was re-probed one UID at a time, on
   precisely the worst run. Now keyed on a dedicated `server_emptying_bodies`.
6. **`transient_fetches` rows were never reclaimed.** `clear_mailbox_labels`
   doesn't touch them, so a UIDVALIDITY reset left stale near-expiry history to
   attach to renumbered UIDs; and rows below the watermark leaked forever, which
   finding 5's probe skip makes routine (expunged UIDs get recorded as held).
   Added `clear_mailbox()` at the reset and `reclaim_below()` at each checkpoint.

**Deferred, filed as [#239](https://github.com/hherb/localmail/issues/239):**
giving up on an unfetchable body leaves no queryable record (no tombstone, no
`list-failed-fetches` / `retry-failed-fetches`). Every sibling failure path in
this codebase keeps a re-drivable row; this one doesn't. Not urgent — pre-#238
the message was *always* silently lost, so this is strictly better, just not yet
as good as the rest of the recovery story.

**Verification (all run this session):**
- `uv run pytest` → **1845 passed** (`test_daemon_control_socket.py` deselected —
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

### 5. **Two new high Dependabot alerts (both in `gui/`, neither in Python)**
   They appeared on this session's push, unrelated to this work:
   - **`quinn-proto`** (Rust, via the Tauri stack) — remote memory exhaustion
     from unbounded out-of-order stream reassembly.
   - **`postcss`** (npm, via the Svelte/Vite toolchain) — path traversal in
     source-map auto-loading, arbitrary `.map` file disclosure.

   **Acceptance:** both resolved by a lockfile bump (`cargo update -p
   quinn-proto`, `npm audit fix` / a targeted `npm update`), with
   `cd gui && npm run check && npm test && npm run build` and
   `cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings`
   green afterwards. Note the pre-existing #90, which wants a broader Tauri
   stack bump for a `glib` unsoundness alert — check whether that subsumes the
   `quinn-proto` one before doing them separately.

### 6. **(Carried) `cargo clippy --all-targets -- -D warnings` fails on `main`**
   `gui/src-tauri/src/commands/search.rs:189` uses `3.14` as a dummy `took_ms`
   → `clippy::approx_constant`. Pre-existing. CI gates clippy **without**
   `--all-targets`, so `#[cfg(test)]` modules are never linted and `main` stays
   green. One-character fix whenever someone is in that file.

## Open decisions & risks

1. **Migration `0033` must be applied to live deployments** before running the
   new code — `sync_mailbox` reads `transient_fetches` on every mailbox pass and
   will error on a DB that has not migrated. Both the macOS and DGX deployments
   need `localmail init-db`.
2. **`max_body_fetch_hold_s` (default 1800 s) is the one number in this change
   chosen by judgement rather than measurement** — watch it in production. It is
   the window over which sync keeps re-trying a message the server won't hand
   over, and it is deliberately a *duration*: an attempt count would be spent at
   the mailbox's IDLE notification rate (see finding 4 above). The cost of a long
   window is that the mailbox's tail is re-fetched on every sync pass while a
   hold is active; the cost of a short one is dropping a message over a blip that
   would have cleared. If either shows up, tune here rather than reinstating a
   count.

   **Second-order:** the give-up leaves no queryable record — filed as
   [#239](https://github.com/hherb/localmail/issues/239), see above.
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
#   expect: 1845 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 124 source files

# Frontend (untouched this session; MUST be run from gui/ — see risk 9):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings && cd ../..
```

`origin/main` at `9565e88`; branch `fix/ingestion-uid-and-message-id` =
`6e059de` + `a650baa` + `40eea4b` + `358f256` + `cce1586` + `afe87f4`, pushed as
PR #238. Latest migration **`0033_transient_fetches.sql`**; next free slot
`0034_*.sql`. Open issues: 21 after #238 merges (#215 and #222 close with it;
#239 filed this session). Dependabot: **2 high** alerts on the default
branch (`quinn-proto`, `postcss` — both `gui/`, no Python exposure), see §5.
