# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-28T0921 UTC.**
> Two commits this session: one handoff/admin (`6abd02a`, branch prune
> after PR #112 squash-merge) and one feature commit on a feature branch
> (`7e61234`, the #115 promotion landing in PR #116 — open, not yet
> merged).
>
> Local branches now at **3**:
>
> - `main` — at `6abd02a` from the start of this session (about to
>   advance with this session's handoff commit).
> - `issue-115-promote-auth-helpers` — tracks live
>   `origin/issue-115-promote-auth-helpers`; PR #116 open against
>   `main`. Kept until #116 merges.
> - `issue-87-at-scale-folder-filter-regression-coverage` — tracks
>   live `origin/issue-87-…`; still active on the remote, kept.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
the DB + attachment tree directly or via the `localmail serve` HTTPS API.
The Tauri 2 + Svelte 5 desktop GUI lives at [gui/](gui/). The HTTPS
admin UI (Sub-plan 1) ships under [src/localmail/serve/admin/](src/localmail/serve/admin/)
and [src/localmail/api/admin/](src/localmail/api/admin/).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md),
[docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md),
[docs/operations/admin-ui-smoke.md](docs/operations/admin-ui-smoke.md),
and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

### Issue + PR

- **Closes #115** (api/auth surface promotion) via **PR #116** (open
  at handoff time — branch `issue-115-promote-auth-helpers` pushed,
  PR opened with TDD-driven new tests + green CI locally).

### Commits (2)

```
7e61234  refactor(auth): promote login-helper private surface to public names (#115)
6abd02a  docs(handoffs): land 2026-05-28T0908 UTC post-PR-112 branch prune snapshot
<new>    docs(handoffs): land 2026-05-28T0921 UTC post-#115 PR-116 snapshot
```

`6abd02a` is on `main` (pushed). `7e61234` is on
`origin/issue-115-promote-auth-helpers` (PR #116, not merged). The
new handoff commit lands on `main`.

### PR #116 (closes #115) — what it does

Promote three login-path primitives from leading-underscore form to
public form in `src/localmail/api/auth.py`, drop the underscore at
every internal caller and admin caller, and keep
identity-preserving aliases at the bottom of the module for one
release per the deprecation window in #115's issue body.

```
_DUMMY_PASSWORD_HASH       → DUMMY_PASSWORD_HASH
_check_login_rate_limits   → check_login_rate_limits
_record_login_attempt      → record_login_attempt
```

The aliases are `name = NEW_NAME` (object identity, not a copy)
so any external caller still importing the old name keeps
working unchanged AND can't drift from the canonical helper.

Sweep / advisory-lock helpers (`_sweep_login_attempts`,
`_maybe_sweep`, `_SWEEP_ADVISORY_LOCK_KEY`) stay private —
no cross-module callers, and #115 explicitly scoped only the
three login-path helpers above.

### Callers migrated

| File | Change |
|------|--------|
| `src/localmail/api/auth.py` | rename + module-bottom alias block + internal callers (`login`, `change_password`) |
| `src/localmail/api/admin/auth.py` | import + caller switched to `DUMMY_PASSWORD_HASH` |
| `src/localmail/serve/admin/auth_router.py` | import + 4 caller sites switched to `check_login_rate_limits` / `record_login_attempt` |
| `tests/test_api_auth_rate_limiter.py` | 33 call sites migrated via prefix-anchored replace (test function names left untouched) |
| `tests/test_serve_auth_routes.py` | import + caller + docstring reference |

### New tests (`tests/test_api_auth_public_surface.py`, 4 cases)

Pins the post-#115 contract end-to-end against the real DB:

1. `test_public_names_exposed_on_module` — the three public names
   exist with the right shape (`isinstance` for the hash, `callable`
   for the helpers).
2. `test_underscored_aliases_resolve_to_public_objects` — identity
   (`is`) check on every alias, so the deprecation window can't
   silently fork the dummy hash.
3. `test_public_dummy_hash_verifies_as_mismatch` — the public hash
   behaves as documented (verifies False for every candidate except
   its own seed).
4. `test_public_record_then_check_round_trip` — recording two
   failures then calling the rate-limit check trips the per-user
   cap end-to-end against the real DB.

### Branch housekeeping (none new this session)

The previous session's prune already returned local branches to the
minimal set. This session adds **one** feature branch
(`issue-115-promote-auth-helpers`, tracks
`origin/issue-115-promote-auth-helpers`) which will be auto-eligible
for pruning once PR #116 merges and `git fetch --prune` marks it
`[gone]`. No manual housekeeping needed at handoff time.

### Verification

- `uv run pytest -q tests/test_api_auth_public_surface.py` — **4
  passed** (the new pinned-surface tests).
- `uv run pytest -q tests/test_api_auth_rate_limiter.py
  tests/test_serve_auth_routes.py` — **34 passed** (existing
  rate-limiter + serve-auth-route tests, now calling the public
  names; identity-preserving aliases prove round-trip).
- `uv run pytest -q tests/` — **896 passed, 6 warnings** (6
  warnings are pre-existing `PytestUnraisableExceptionWarning`
  from `psycopg_pool.ConnectionPool.__del__`, unrelated to this
  PR — they appear on a clean `main` too).
- `uv run mypy src/localmail/api/auth.py
  src/localmail/api/admin/auth.py
  src/localmail/serve/admin/auth_router.py
  tests/test_api_auth_public_surface.py` — **Success: no issues
  found**.
- `uv run mypy src/localmail` — 4 pre-existing errors in
  `parser.py` only (confirmed via `git stash` round-trip against
  clean `main`; out of scope for #115).
- `git status` — clean (only `.claude/settings.local.json`
  untracked, by design).

### Docs

- **NEXT_SESSION.md** — *replaced this session* with the current
  state (this file).
- **docs/handoffs/2026-05-28T0921-utc-post-pr-116-issue-115-promotion.md** —
  *new this session* (this file's frozen snapshot).
- **README.md** — *unchanged*. The promotion is an internal API
  surface rename with identity-preserving aliases; no user-facing
  behavior or CLI change. PR #112's earlier merge similarly didn't
  touch README.
- **CLAUDE.md** — *unchanged*. No new cross-cutting runtime
  invariant. The promotion is a private→public surface rename, not
  a behavior change.
- **ROADMAP.md** — does not exist in this repo. Not created
  (same decision as prior sessions).

## What's next

### 1. **Wait for PR #116 review/merge** *(immediate)*

If review feedback lands on PR #116, address it on
`issue-115-promote-auth-helpers`, push, ping. Once merged:

- `git fetch --prune` will mark
  `origin/issue-115-promote-auth-helpers` as `[gone]`.
- `commit-commands:clean_gone` skill will sweep the local branch
  automatically.
- `_DUMMY_PASSWORD_HASH` / `_check_login_rate_limits` /
  `_record_login_attempt` aliases stay one release per the
  deprecation policy; a follow-up issue to drop them after one
  release lands could be filed if desired, but is **not** required
  by #115's acceptance — the issue body only mandates the
  one-release alias window, not the eventual removal.

### 2. **Remaining PR #112 follow-ups** *(still open)*

- **#114** admin UI: `state_signing_key` required at `create_app()`
  even though Sub-plan 1 doesn't use it. Resolution likely deferred
  to Sub-plan 4, where the consumer lives.
- **#113** admin UI: no server-side session revocation. Likely
  Sub-plan 2 candidate via `api_users.sessions_invalidated_at`
  (option 1 in the issue body).

### 3. **Carried-forward deferred items** *(unchanged — still externally blocked)*

- **#47** `extract_worker` transient-class opt-in for third
  parties — needs production telemetry.
- **#90** glib Cargo alert — upstream-blocked (Tauri stack bump).
  Dependabot reminder banner during `git push` is the same one.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 7** (was 7 last session; #115 still counted as
open until PR #116 merges, at which point it becomes 6).

### 4. Acceptance criteria for the most likely next concrete task

If the next session picks up **#113** (Sub-plan 2 candidate,
post-PR-116-merge):

- Add migration `0022_api_users_sessions_invalidated_at.sql`
  introducing the timestamptz column (default NULL).
- Extend `get_admin_user` /
  `localmail.api.admin.session_tokens.decode_session_token` to
  reject tokens whose `issued_at < sessions_invalidated_at`.
- Add a `localmail revoke-admin-sessions USERNAME` CLI command.
- Write a regression test in `tests/test_admin_session_tokens.py`
  that issues a token, then bumps `sessions_invalidated_at`, then
  confirms the next `require_admin_session` request 401s.
- Acceptance: `uv run pytest tests/test_admin_*.py
  tests/test_serve_admin_*.py` green; mypy clean on touched
  files; one paragraph in CLAUDE.md only if a new operator-facing
  invariant emerges (a `sessions_invalidated_at` column likely
  warrants a one-liner under "Schema essentials").

If telemetry arrives for **#47** instead — same plan as the prior
handoff (extend transient classifier in
`src/localmail/search/extract_worker.py`, regression test in
`tests/test_extract_worker.py`).

## Open decisions & risks

1. **PR #116 still open at handoff time.** Pre-flight self-review
   is clean and CI gates locally (`pytest`, `mypy`) all green.
   Operator-facing risk is zero — identity-preserving aliases mean
   no caller breaks, and the three new public names don't change
   any wire/DB/CLI behavior. If you push more commits to the
   feature branch and reuse this handoff, update the commit count
   above accordingly.

2. **`_DUMMY_PASSWORD_HASH` alias seed value**. The deprecation
   alias is `_DUMMY_PASSWORD_HASH = DUMMY_PASSWORD_HASH` (module-
   level assignment), which means both names point at the same
   argon2id-hashed dummy seed string. If any future code wants to
   re-seed the dummy hash on every import or per-process, the
   alias must be re-evaluated at the same time as the canonical
   name — currently neither is re-evaluated (the seed is fixed at
   module-import time, by design).

3. **CLAUDE.md not updated for PR #116.** Deliberate: a
   private→public surface rename with identity aliases is not a
   new architectural invariant. If a future PR drops the aliases
   it might warrant a single-line CLAUDE.md note ("api/auth
   public surface is X / Y / Z; do not import underscored
   names"), but landing that now would be premature.

4. **`.claude/settings.local.json` stays untracked.** Same as
   prior sessions — local-only file.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect clean working tree (only .claude/* untracked)
git log --oneline -4                 # tip on main:
                                     #   <new>     docs(handoffs): land 2026-05-28T0921 UTC post-#115 PR-116 snapshot
                                     #   6abd02a   docs(handoffs): land 2026-05-28T0908 UTC post-PR-112 branch prune snapshot
                                     #   6e20be9   feat(admin): admin UI Sub-plan 1 — auth scaffolding (#112)
                                     #   b95a105   docs(handoffs): land 2026-05-27T2202 UTC orphan-branch-prune snapshot
git branch -vv                       # expect 3 entries (main + issue-115-… + issue-87-…)
gh pr list --state open --limit 5    # expect #116 open
gh issue list --state open --limit 40   # expect 7 open (#5, #25, #47, #90, #113, #114, #115)

# If PR #116 has merged when you resume:
git checkout main && git pull origin main
git fetch --prune origin            # marks issue-115-… [gone]
# `clean_gone` skill (or git branch -D issue-115-promote-auth-helpers) does the local prune.

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (post-#116: 896 passed expected)
unset VIRTUAL_ENV && uv run pytest -q tests/test_api_auth_public_surface.py tests/test_api_auth_rate_limiter.py tests/test_serve_auth_routes.py
unset VIRTUAL_ENV && uv run mypy src/localmail/api/auth.py src/localmail/api/admin/auth.py src/localmail/serve/admin/auth_router.py
cd gui && npm test -- --run                         # GUI client (last verified: 312 passed)
cd gui && npm run check                             # GUI typecheck (last verified: 0 errors)
```

If picking up **#113** (next-most-likely Sub-plan 2 candidate):

```bash
git checkout -b issue-113-admin-session-revocation
# add migrations/0022_api_users_sessions_invalidated_at.sql
# extend src/localmail/api/admin/session_tokens.py + get_admin_user
# add CLI: src/localmail/cli.py revoke-admin-sessions
# add tests under tests/test_admin_session_tokens.py +
#   tests/test_serve_admin_session_revocation.py
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_*.py tests/test_serve_admin_*.py
unset VIRTUAL_ENV && uv run mypy src/localmail
```

## File map (post-session)

```
NEXT_SESSION.md                                                       # REPLACED this session
docs/handoffs/
  2026-05-28T0921-utc-post-pr-116-issue-115-promotion.md              # NEW (this session's snapshot)
  2026-05-28T0908-utc-post-pr-112-branch-prune.md                     # prior (earlier this session)
  2026-05-27T2202-utc-orphan-branch-prune.md                          # prior
  2026-05-27T1438-utc-stale-branch-prune.md                           # earlier
  2026-05-27T1427-utc-pr-111-followup-verified.md                     # earlier
  2026-05-27T1409-utc-gui-stale-comments-pr-111.md                    # earlier
  …
```

`main` is at `<new>` after this session's handoff commit. Working
tree clean (only `.claude/settings.local.json` untracked, by
design). 3 local branches (`main`, `issue-115-…`, `issue-87-…`);
PR #116 open against `main`.
