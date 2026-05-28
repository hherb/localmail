# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-28T2255 UTC.**
> PR #118 (closes #114) **already merged** into `main` as squash
> commit `26fca84` (before this session opened) — Sub-plan 2A:
> DB-canonical accounts + account-management CRUD service + web
> Gmail OAuth flow with HMAC-signed state. This session:
> pruned the stale local `sub-plan-2a-account-management` branch
> and its worktree (`worktree-sub-plan-2a-account-management`),
> both tracking the now-deleted `origin/sub-plan-2a-account-management`;
> committed the previously-untracked Sub-plan 2A implementation
> plan as the audit record for #118; re-ran the full suite on
> `main` to confirm green; and rolled the handoff forward.
>
> Local branches now at **2**:
>
> - `main` — at `26fca84` (post-merge of PR #118), about to advance
>   with this session's handoff + plan-file commit.
> - `issue-87-at-scale-folder-filter-regression-coverage` — tracks
>   live `origin/issue-87-…`; still active on the remote, kept.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
the DB + attachment tree directly or via the `localmail serve` HTTPS API.
The Tauri 2 + Svelte 5 desktop GUI lives at [gui/](gui/). The HTTPS
admin UI (Sub-plans 1 + 2A to date — auth scaffolding, session
revocation, and now account management + web OAuth) ships under
[src/localmail/serve/admin/](src/localmail/serve/admin/) and
[src/localmail/api/admin/](src/localmail/api/admin/), with the
end-to-end design captured in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md),
[docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md),
[docs/operations/admin-ui-smoke.md](docs/operations/admin-ui-smoke.md),
and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

A **maintenance / handoff-roll** session — no production code or
docs landed beyond NEXT_SESSION.md, its frozen snapshot, and the
previously-untracked Sub-plan 2A implementation plan (committed for
audit parity with the other tracked plans).

### Commits (post-PR-118-merge on main)

```
26fca84  feat(admin): Sub-plan 2A — account management + web OAuth (closes #114) (#118)
0f0a96e  docs(handoffs): backfill handoff SHA b542d41 into NEXT_SESSION + snapshot
b542d41  docs(handoffs): land 2026-05-28T1241 UTC post-PR-117-merge snapshot
53fcaa5  feat(admin): server-side session revocation via sessions_invalidated_at (#113) (#117)
a32550c  docs(handoffs): land 2026-05-28T0936 UTC post-PR-116-merge snapshot
```

### Branch & worktree maintenance

- `git fetch --prune origin` — confirmed
  `origin/sub-plan-2a-account-management` was deleted after the
  squash-merge of PR #118 (the prune line `[deleted] … ->
  origin/sub-plan-2a-account-management` printed).
- Verified the final PR head `ff00875` (local
  `sub-plan-2a-account-management`, with the
  `fixup(admin): address Sub-plan 2A code-review findings` commit)
  is **content-identical to `main`** (`git diff --stat main
  sub-plan-2a-account-management` empty) before deleting anything.
- `git branch -D sub-plan-2a-account-management` — local prune
  done this session (was `ff00875`).
- `git worktree remove --force
  .claude/worktrees/sub-plan-2a-account-management` +
  `git branch -D worktree-sub-plan-2a-account-management` (was
  `ebeace8`) + `git worktree prune` — the worktree only held an
  untracked local-only `.claude/settings.local.json`, and its
  branch was fully merged, so `--force` was safe.
- Committed `docs/superpowers/plans/2026-05-28-admin-ui-sub-plan-2a.md`
  — it shipped untracked alongside PR #118; every sibling plan
  (e.g. `2026-05-28-admin-ui-sub-plan-1-auth-scaffolding.md`) is
  tracked, so this restores audit parity.

### Recap of what PR #118 (closing #114) delivered

Already merged before this session opened; recapped here so the
hand-off is self-contained. Authoritative detail lives in the
"GUI server" section of CLAUDE.md ("DB-canonical accounts + admin
CRUD (Sub-plan 2A)").

- Migration `0020_accounts_canonical.sql` makes `accounts` the
  write-authoritative store for IMAP config: adds `folder_allow`,
  `folder_deny`, `folder_deny_flags` (RFC 6154 flag-based denial),
  `sync_enabled`, `updated_at`; lifts NOT NULL from
  `imap_host`/`imap_port`; widens `auth_method` to include
  `'archive'`; adds the `accounts_live_requires_host` check
  constraint. (Fills the long-reserved `0020` gap.)
- `src/localmail/api/admin/accounts.py` — transport-free service
  layer: `list_accounts`, `get_account`, `create_account`,
  `update_account`, `delete_account` (cascade-or-refuse unless
  `force=True`), `store_password`, `clear_secret`,
  `probe_connection` (renamed from `test_connection` to dodge
  pytest auto-collection).
- `src/localmail/api/admin/oauth.py` + `oauth_state.py` — web
  Gmail OAuth: `start_oauth` returns a Google consent URL and
  writes a stateless HMAC-signed state token
  (`encode_state`/`decode_state`: JSON payload +
  `base64url(hmac_sha256(key, payload))`); `complete_oauth`
  verifies state, exchanges the code, persists the refresh token.
  **This is the real consumer of `[serve].state_signing_key` —
  closes #114.**
- HTTP routes: CRUD + password + test-connection under
  `/v1/admin/accounts` (URL keeps `test-connection` even though
  the Python fn is `probe_connection`); OAuth at `POST
  /v1/admin/accounts/{id}/oauth/start` and `GET
  /admin/oauth/callback`. The callback reads `state`/`code` via
  `get_unscrubbed_query_params(request)` because the
  scrub-sensitive-query-params middleware would otherwise redact
  them.
- Cookie `Path` widened to `"/"` so the admin session cookie
  reaches `/v1/admin/*`; SameSite=Lax + per-route CSRF tokens
  remain the primary CSRF defences. No `/v1/*` machine endpoint
  reads cookies.
- **Deferred to Sub-plan 2A.2**: rewiring CLI `add-account`,
  `oauth-login`, `remove-account` to write to the DB; TOML→DB seed
  at `init-db`. The v1 daemon does **not** yet honour
  `sync_enabled`.

### Verification on main (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **965 passed,
  9 warnings in 67.18s** on `26fca84`. (+56 vs the pre-merge 909 —
  PR #118 added `tests/test_admin_accounts.py` and
  `tests/test_serve_admin_accounts.py`.) The 9 warnings are
  upstream / teardown cosmetics: `websockets.legacy`
  DeprecationWarnings (#25), the `psycopg_pool.ConnectionPool.__del__`
  `PytestUnraisableExceptionWarning`, a `RuntimeError: cannot join
  current thread` teardown irritant on the real-model embeddings
  smoke test, and a `hf_xet.download_files()` DeprecationWarning
  from the fastembed model download. None indicate a leak or a
  product bug.
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **4 errors,
  all in `src/localmail/parser.py`** (`union-attr` /`arg-type`
  around `_decode_part_text` and `Attachment(payload=…)`).
  **These are PRE-EXISTING on `main`, not a #118 regression and
  not from this session** — `parser.py` is byte-identical at the
  pre-#118 commit `0f0a96e` (verified by `git show
  0f0a96e:src/localmail/parser.py`), and #118 never touched it.
  See "Open decisions & risks" #1.
- `git status` — clean apart from the two untracked-by-design
  paths (`.claude/settings.local.json`, plus the plan file this
  session is about to commit).
- `git branch -vv` — 2 entries: `main` (at `26fca84`) +
  `issue-87-…` (still tracked).
- `git worktree list` — single entry (`main`); the Sub-plan 2A
  worktree is pruned.
- `gh issue list --state open` — **9 open issues** (#5, #25, #47,
  #90, #119, #120, #121, #122, #123); #114 closed automatically
  via "closes #114" in the PR #118 body.
- `gh pr list --state open` — **0 open PRs**.

### Docs

- **NEXT_SESSION.md** — *replaced this session* with the current
  state (this file).
- **docs/handoffs/2026-05-28T2255-utc-post-pr-118-merge-branch-prune.md**
  — *new this session* (this file's frozen snapshot).
- **docs/superpowers/plans/2026-05-28-admin-ui-sub-plan-2a.md** —
  *committed this session* (was untracked; audit parity with
  sibling plans).
- **README.md** — *unchanged*. PR #118 deferred all CLI rewiring
  to Sub-plan 2A.2, so no new user-facing command surface shipped;
  the admin operator commands stay undocumented in the README
  while the admin UI is mid-rollout (same conservative posture as
  the #115/#116/#117 handoffs).
- **CLAUDE.md** — *unchanged this session*. PR #118 already added
  the authoritative "DB-canonical accounts + admin CRUD (Sub-plan
  2A)" paragraph and the `0020_accounts_canonical.sql` entry in
  the migration index.
- **ROADMAP.md** — does not exist in this repo. Not created (same
  decision as prior sessions).

## What's next

PR #118's review filed **five small, well-scoped follow-ups**
(#119–#123) — these are the first time in several sessions there
are concrete "grab-and-ship" tickets rather than externally-blocked
ones. They could be bundled into one cleanup PR or folded into the
start of Sub-plan 2A.2. After those, the natural arc continues
through the remaining admin-UI sub-plans from
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md).

### 1. **Sub-plan 2A review follow-ups (#119–#123)** *(recommended next code-bearing session — small cleanups)*

- **#119** — Refactor the accounts SELECT to keyword construction
  (replace `Account(*row)` positional unpack). *Acceptance:* the
  accounts row→model path constructs `Account(**{...})` (or named
  kwargs) so column-order drift can't silently mis-map fields;
  existing `tests/test_admin_accounts.py` stays green.
- **#120** — Thread `gmail_oauth.client_secrets_file` into the
  admin OAuth flow instead of calling `load_config()` per request.
  *Acceptance:* `start_oauth`/`complete_oauth` receive the secrets
  path via dependency/parameter; no `load_config()` call inside
  the request path; a test asserts the injected path is used.
- **#121** — Pin the invariant that **no `/v1/*` route reads the
  admin session cookie** (Path=/ scope guard). *Acceptance:* a
  regression test enumerates `/v1/*` routes and asserts none
  depend on the admin-cookie dependency; documents the guard
  next to the cookie-Path note.
- **#122** *(cosmetic)* — Distinct CSRF action strings per HTTP
  method on shared paths. *Acceptance:* CSRF token binding
  includes the method (or a per-method action constant) so a
  token minted for `POST <path>` can't be replayed against
  `DELETE <path>`; CSRF tests cover the method dimension.
- **#123** — `AccountInUse` should subclass `ValueError` to match
  the `AccountFieldError` parent. *Acceptance:* `AccountInUse`
  MRO includes `ValueError`; existing `except`-site behaviour
  unchanged; a test pins the subclass relationship.

All five are confined to `src/localmail/api/admin/` +
`src/localmail/serve/admin/` + their tests; mypy clean on touched
files; one-line CLAUDE.md note only if a new cross-cutting
invariant is introduced (likely just #121's Path=/ guard).

### 2. **Admin-UI Sub-plan 2A.2 / 2A.3 / 2B / 2C** *(larger arc)*

From [docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md):

- **2A.2** — Rewire CLI `add-account` / `oauth-login` /
  `remove-account` to write to the DB; TOML→DB seed at `init-db`;
  make the v1 daemon honour `sync_enabled`.
- **2A.3** — Jinja2/HTMX UI screens for accounts (design doc § 4
  templates).
- **2B** — Daemon control (`DaemonSupervisor` + HTTP shape) —
  needs migration `0024_daemon_heartbeats.sql` (renumbered; see
  risk #2).
- **2C** — mbox import (`ImportWorker` + `ImportWorkerSupervisor`)
  — needs migration `0024+_import_jobs.sql` (renumbered; see risk
  #2).

`0021_api_users_admin.sql` from the design doc is still free; if
it didn't ship with Sub-plan 1, confirm before allocating.

### 3. **Carried-forward deferred items** *(unchanged — still externally blocked)*

- **#47** `extract_worker` transient-class opt-in for third
  parties — needs production telemetry.
- **#90** glib Cargo alert — upstream-blocked (Tauri stack bump).
  Same Dependabot reminder banner during `git push`.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
  Visible in the pytest output among the 9 warnings.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 9** (was 5 last session; #114 closed by PR
#118 merge, but the #118 review added #119–#123).

## Open decisions & risks

1. **Pre-existing mypy errors in `src/localmail/parser.py` (4).**
   `union-attr`/`arg-type` around `_decode_part_text(part:
   EmailMessage)` being called with `MIMEPart`, and
   `Attachment(payload=…)` getting a `Message | bytes | Any`.
   Confirmed present at `0f0a96e` (pre-#118), so a longstanding
   typing-debt item, **not** introduced by Sub-plan 2A. The
   handoff's historical "mypy clean" claims were scoped to each
   PR's *touched* files; the whole-tree `mypy src/localmail` has
   carried these for a while. Worth a small dedicated typing-fix
   PR (tighten `email.message` types in `parser.py`) but out of
   scope for any admin-UI ticket. Flagging so the next session
   doesn't mistake it for a regression in their own work.

2. **Migration renumbering for Sub-plan 2B/2C.** The admin-UI
   design doc allocated `0020`-`0023`. Shipped so far: `0020`
   (accounts_canonical, this PR), `0022`
   (api_users_sessions_invalidated_at, PR #117). So the design
   doc's `0022_import_jobs` and `0023_daemon_heartbeats` must
   slide — next free slots are `0023`/`0024` (verify `0021` and
   `0023` are actually free at plan-time with `ls migrations/`).
   Easy to miss when copy-pasting from the design doc.

3. **README still does not mention admin operator commands**
   (`grant-admin`, `revoke-admin`, `revoke-admin-sessions`, and
   the not-yet-built account CLI). Deliberate while the admin UI
   is mid-rollout. Revisit once 2A.2 (CLI rewiring) lands — that
   is the natural moment to document the admin command surface,
   either in README or in
   [docs/operations/admin-ui-smoke.md](docs/operations/admin-ui-smoke.md).

4. **`.claude/settings.local.json` stays untracked.** Same as
   prior sessions — local-only file.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect clean working tree (only .claude/settings.local.json untracked)
git log --oneline -5                 # tip on main:
                                     #   <this session's handoff commit>
                                     #   26fca84   feat(admin): Sub-plan 2A — account management + web OAuth (closes #114) (#118)
                                     #   0f0a96e   docs(handoffs): backfill handoff SHA b542d41 into NEXT_SESSION + snapshot
                                     #   b542d41   docs(handoffs): land 2026-05-28T1241 UTC post-PR-117-merge snapshot
                                     #   53fcaa5   feat(admin): server-side session revocation via sessions_invalidated_at (#113) (#117)
git branch -vv                       # expect 2 entries (main + issue-87-…)
git worktree list                    # expect 1 entry (main)
gh pr list --state open --limit 5    # expect 0 open
gh issue list --state open --limit 40   # expect 9 open (#5, #25, #47, #90, #119, #120, #121, #122, #123)

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (expect 965 passed)
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_*.py tests/test_serve_admin_*.py
unset VIRTUAL_ENV && uv run mypy src/localmail      # NOTE: 4 pre-existing parser.py errors (see risk #1)
cd gui && npm test -- --run                         # GUI client (last verified: 312 passed)
cd gui && npm run check                             # GUI typecheck (last verified: 0 errors)
```

If picking up the **#119–#123 cleanups** (recommended — small,
self-contained):

```bash
git checkout -b sub-plan-2a-review-followups
# Work is confined to src/localmail/api/admin/ + src/localmail/serve/admin/ + tests.
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_*.py tests/test_serve_admin_*.py
unset VIRTUAL_ENV && uv run mypy src/localmail/api/admin src/localmail/serve/admin
```

If picking up **Sub-plan 2A.2** (CLI rewiring + TOML→DB seed):

```bash
git checkout -b sub-plan-2a2-cli-db-rewire
# Plan first under docs/superpowers/plans/, drawing from
# docs/superpowers/specs/2026-05-28-admin-ui-design.md § 2A.2.
# Renumber any new migration to the next free slot (0020 + 0022 are taken).
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
```

## File map (post-session)

```
NEXT_SESSION.md                                                       # REPLACED this session
docs/superpowers/plans/
  2026-05-28-admin-ui-sub-plan-2a.md                                 # COMMITTED this session (was untracked; #118 audit record)
docs/handoffs/
  2026-05-28T2255-utc-post-pr-118-merge-branch-prune.md              # NEW (this session's snapshot)
  2026-05-28T1241-utc-post-pr-117-merge-branch-prune.md              # prior (PR #117 merge)
  2026-05-28T0936-utc-post-pr-116-merge.md                           # prior
  2026-05-28T0921-utc-post-pr-116-issue-115-promotion.md             # prior
  2026-05-28T0908-utc-post-pr-112-branch-prune.md                    # prior
  …
```

`main` advances with this session's handoff + plan-file commit.
Working tree otherwise clean (only `.claude/settings.local.json`
untracked, by design). 2 local branches (`main`, `issue-87-…`);
1 worktree (`main`); no open PRs.
