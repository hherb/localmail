# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-28T1241 UTC.**
> PR #117 (closes #113) **already merged** into `main` as squash
> commit `53fcaa5` (before this session opened). This session:
> pruned the stale local `issue-113-admin-session-revocation`
> branch (its remote was `[gone]` after the squash-merge),
> re-ran the full suite on `main` to confirm green, and rolled
> the handoff forward. Working tree clean.
>
> Local branches now at **2**:
>
> - `main` — at `53fcaa5` (post-merge of PR #117), about to advance
>   with this session's handoff commit.
> - `issue-87-at-scale-folder-filter-regression-coverage` — tracks
>   live `origin/issue-87-…`; still active on the remote, kept.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
the DB + attachment tree directly or via the `localmail serve` HTTPS API.
The Tauri 2 + Svelte 5 desktop GUI lives at [gui/](gui/). The HTTPS
admin UI (#112 + #117 to date — auth scaffolding + session revocation)
ships under [src/localmail/serve/admin/](src/localmail/serve/admin/)
and [src/localmail/api/admin/](src/localmail/api/admin/), with the
end-to-end design captured in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md),
[docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md),
[docs/operations/admin-ui-smoke.md](docs/operations/admin-ui-smoke.md),
and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

This is a **maintenance / handoff-roll** session — no code or
docs landed beyond NEXT_SESSION.md and its frozen snapshot.

### Commits (post-PR-117-merge on main)

```
<new>    docs(handoffs): land 2026-05-28T1241 UTC post-PR-117-merge snapshot
53fcaa5  feat(admin): server-side session revocation via sessions_invalidated_at (#113) (#117)
a32550c  docs(handoffs): land 2026-05-28T0936 UTC post-PR-116-merge snapshot
1d036af  refactor(auth): promote login-helper private surface to public names (#115) (#116)
c479a1c  docs(handoffs): land 2026-05-28T0921 UTC post-#115 PR-116 snapshot
```

### Branch maintenance

- `git fetch --prune origin` — confirmed
  `origin/issue-113-admin-session-revocation` is `[gone]` after the
  squash-merge of PR #117.
- `git branch -D issue-113-admin-session-revocation` — local prune
  done this session (was `d058245`).

### Recap of what PR #117 (closing #113) delivered

Already merged before this session opened; recapped here so the
hand-off is self-contained.

- Migration `0022_api_users_sessions_invalidated_at.sql` adds a
  nullable `sessions_invalidated_at TIMESTAMPTZ` column on
  `api_users`. Default NULL = "never revoked".
- Admin-cookie dependency
  (`localmail.serve.admin.dependencies.require_admin_session`)
  now passes the session token's `issued_at` into
  `get_admin_user`. The service does
  `to_timestamp(issued_at) < sessions_invalidated_at` in the same
  SELECT and raises `SessionInvalidated` when the token predates
  the revocation moment — translated to a 303 redirect to
  `/admin/login`.
- New CLI `localmail revoke-admin-sessions USERNAME` bumps the
  column to `now()`. Admin privileges themselves are untouched
  (operators use the pre-existing `revoke-admin` for that).
- Check is opt-in: callers that don't pass `issued_at` (CLI
  lookups, smoke paths) skip the comparison entirely so they
  keep working on a revoked user.
- CLAUDE.md "GUI server" section already carries the
  authoritative one-paragraph note (lines 483-497 of CLAUDE.md).

### Verification on main (this session)

- `uv run pytest -q tests/` — **909 passed, 4 warnings in 43.83s**
  on `53fcaa5`. (Count is +12 vs the pre-merge 897 because the
  #113 work added regression tests around the
  `sessions_invalidated_at` boundary, the precision-boundary
  acceptance, and the CLI surface.) The 4 warnings are unrelated
  upstream issues — 2× `websockets.legacy`
  `DeprecationWarning` (#25), 1× `protocol_type` notice on the
  same path, and 1× `psycopg_pool.ConnectionPool.__del__`
  `PytestUnraisableExceptionWarning` (teardown irritant —
  cosmetic, not a leak).
- `git status` — clean (only `.claude/settings.local.json`
  untracked, by design).
- `git branch -vv` — 2 entries: `main` (at `53fcaa5`) +
  `issue-87-…` (still tracked).
- `gh issue list --state open` — **5 open issues** (#5, #25,
  #47, #90, #114); #113 closed automatically via "Closes #113"
  in the PR #117 body.
- `gh pr list --state open` — **0 open PRs**.

### Docs

- **NEXT_SESSION.md** — *replaced this session* with the current
  state (this file).
- **docs/handoffs/2026-05-28T1241-utc-post-pr-117-merge-branch-prune.md** —
  *new this session* (this file's frozen snapshot).
- **README.md** — *unchanged*. The new admin operator commands
  (`grant-admin`, `revoke-admin`, `revoke-admin-sessions`) are
  deliberately not yet documented in the user-facing serve
  command table because the admin UI is mid-rollout (multiple
  open Sub-plan-2/3 follow-ups per the design doc, plus the
  still-open #114 about `state_signing_key` plumbing). Same
  conservative posture as the #115 / PR-116 handoff.
- **CLAUDE.md** — *unchanged this session*. The #113 note was
  added by PR #117 itself; the migration index already mentions
  `0022_api_users_sessions_invalidated_at.sql`.
- **ROADMAP.md** — does not exist in this repo. Not created
  (same decision as prior sessions).

## What's next

Every open issue is either externally blocked, deferred until
measurement, or a known follow-up to land alongside future
admin-UI sub-plans. There is no "obvious next ticket to grab
and ship" — the natural next move is to begin the next admin-UI
sub-plan from
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md),
which would also resolve #114 along the way.

### 1. **Admin-UI Sub-plan 2 / 3** *(recommended next code-bearing session)*

The admin UI design doc enumerates four schema additions still
unshipped (`0020_accounts_canonical.sql`,
`0021_api_users_admin.sql`, `0022_import_jobs.sql`,
`0023_daemon_heartbeats.sql`) and three feature areas:

- **2A. Account management** (canonical accounts CRUD, OAuth
  flow with HMAC-signed state — wires the still-required
  `state_signing_key` config — closes **#114**).
- **2B. Daemon control** (`DaemonSupervisor` + HTTP shape).
- **2C. mbox import** (`ImportWorker` + `ImportWorkerSupervisor`).

CLAUDE.md notes the migration gap explicitly: *"(The gap at
`0020_*` is reserved for the unshipped `accounts_canonical`
migration planned by the admin UI design doc.)"* — so when
starting Sub-plan 2A, **renumber carefully**: the next
migration to land will be `0020_accounts_canonical.sql`, and
`0021`, `0022_import_jobs.sql`, `0023_daemon_heartbeats.sql`
follow per the design doc. The already-shipped
`0022_api_users_sessions_invalidated_at.sql` consumed
`0022` first — so `import_jobs` will need to renumber to the
next free slot (`0024_*` or higher) at plan-time.

Acceptance for kicking off **Sub-plan 2A** specifically:

```
- migrations/0020_accounts_canonical.sql exists, applies cleanly
  on a fresh DB and on a live archive (see upgrade-runbook).
- src/localmail/api/admin/accounts.py exposes list/get/create/
  update/delete service functions (no FastAPI imports).
- src/localmail/serve/admin/accounts_router.py exposes the HTTP
  shape from the design doc § 2A "HTTP shape".
- HMAC-signed OAuth state path consumes the
  config.admin.state_signing_key — fixing #114.
- Regression tests under tests/test_admin_accounts.py and
  tests/test_serve_admin_accounts.py.
- mypy clean on touched files.
- One-line CLAUDE.md note added for any new cross-cutting
  runtime invariant (e.g. OAuth state HMAC scheme).
```

### 2. **#114** admin UI: `state_signing_key` config plumbing

The key is already required at `create_app()` even though no
shipping endpoint consumes it. The cleanest resolution is the
HMAC-signed OAuth state path in Sub-plan 2A above; leaving it
required-but-unused is benign in the meantime.

### 3. **Carried-forward deferred items** *(unchanged — still externally blocked)*

- **#47** `extract_worker` transient-class opt-in for third
  parties — needs production telemetry.
- **#90** glib Cargo alert — upstream-blocked (Tauri stack
  bump). Dependabot reminder banner during `git push` is the
  same one.
- **#25** websockets.legacy DeprecationWarning — upstream-
  blocked. Visible in the pytest output as 2 of the 4 warnings.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 5** (was 6 last session; #113 closed by
PR #117 merge).

## Open decisions & risks

1. **Migration renumbering at the start of Sub-plan 2A.**
   The admin-UI design doc allocates `0020`-`0023`. We have
   already shipped `0022_api_users_sessions_invalidated_at.sql`
   between then and now, so the *design doc's* `0022_import_jobs`
   and `0023_daemon_heartbeats` need to slide by one slot at
   plan-time. Easy to miss when copy-pasting from the design
   doc — flag it in the implementation plan.

2. **README still does not mention any of `grant-admin`,
   `revoke-admin`, or `revoke-admin-sessions`.** Deliberate:
   the admin UI is mid-rollout. Either add all three (plus the
   `localmail serve` admin-UI mount instructions) once the next
   sub-plan lands, or leave the README focused on the
   programmatic / `serve` surface and document the admin
   operator commands inside
   [docs/operations/admin-ui-smoke.md](docs/operations/admin-ui-smoke.md)
   instead.

3. **`.claude/settings.local.json` stays untracked.** Same as
   prior sessions — local-only file.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect clean working tree (only .claude/* untracked)
git log --oneline -5                 # tip on main:
                                     #   <new>     docs(handoffs): land 2026-05-28T1241 UTC post-PR-117-merge snapshot
                                     #   53fcaa5   feat(admin): server-side session revocation via sessions_invalidated_at (#113) (#117)
                                     #   a32550c   docs(handoffs): land 2026-05-28T0936 UTC post-PR-116-merge snapshot
                                     #   1d036af   refactor(auth): promote login-helper private surface to public names (#115) (#116)
                                     #   c479a1c   docs(handoffs): land 2026-05-28T0921 UTC post-#115 PR-116 snapshot
git branch -vv                       # expect 2 entries (main + issue-87-…)
gh pr list --state open --limit 5    # expect 0 open
gh issue list --state open --limit 40   # expect 5 open (#5, #25, #47, #90, #114)

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (expect 909 passed)
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_*.py tests/test_serve_admin_*.py
unset VIRTUAL_ENV && uv run mypy src/localmail
cd gui && npm test -- --run                         # GUI client (last verified: 312 passed)
cd gui && npm run check                             # GUI typecheck (last verified: 0 errors)
```

If picking up **Sub-plan 2A** (the next-most-likely code-bearing
session — closes #114 along the way):

```bash
git checkout -b sub-plan-2a-account-management
# Plan first under docs/superpowers/plans/, drawing from
# docs/superpowers/specs/2026-05-28-admin-ui-design.md §§ 2A + 3.
# Renumber 0022_import_jobs / 0023_daemon_heartbeats — slot 0022
# is taken (sessions_invalidated_at). 0020 accounts_canonical and
# 0021 api_users_admin remain free.
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_*.py tests/test_serve_admin_*.py
unset VIRTUAL_ENV && uv run mypy src/localmail
```

## File map (post-session)

```
NEXT_SESSION.md                                                       # REPLACED this session
docs/handoffs/
  2026-05-28T1241-utc-post-pr-117-merge-branch-prune.md               # NEW (this session's snapshot)
  2026-05-28T0936-utc-post-pr-116-merge.md                            # prior (PR #116 merge)
  2026-05-28T0921-utc-post-pr-116-issue-115-promotion.md              # prior
  2026-05-28T0908-utc-post-pr-112-branch-prune.md                     # prior
  2026-05-27T2202-utc-orphan-branch-prune.md                          # prior
  2026-05-27T1438-utc-stale-branch-prune.md                           # earlier
  2026-05-27T1427-utc-pr-111-followup-verified.md                     # earlier
  …
```

`main` is at `<new>` after this session's handoff commit. Working
tree clean (only `.claude/settings.local.json` untracked, by
design). 2 local branches (`main`, `issue-87-…`); no open PRs.
