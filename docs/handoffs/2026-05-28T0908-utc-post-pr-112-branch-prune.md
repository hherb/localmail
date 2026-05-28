# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-28T0908 UTC.**
> **No source changes this session — local + remote branch housekeeping
> only, after PR #112 (admin UI Sub-plan 1) merged in a prior session.**
>
> Local branches now reduced to **2** (was 4):
>
> - `main` — at `6e20be9` from PR #112 (about to advance with this
>   session's handoff commit).
> - `issue-87-at-scale-folder-filter-regression-coverage` — tracks
>   live `origin/issue-87-…`; still active on the remote, kept.
>
> Remote branches similarly trimmed: `origin/admin-ui-sub-plan-1`
> deleted now that PR #112 is squash-merged into `main`.

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

- **No new issue, no new PR.** Session was branch housekeeping in
  the wake of PR #112's squash merge (touches only `NEXT_SESSION.md`
  + this session's handoff snapshot under `docs/handoffs/`).

### Commits (1 — handoff/admin only)

```
<filled in after `git commit`>  docs(handoffs): land 2026-05-28T0908 UTC post-PR-112 branch prune snapshot
```

The single commit lands this updated `NEXT_SESSION.md` plus its
frozen archive under `docs/handoffs/`. No source-tree changes.

### Branch housekeeping (3 merged branches deleted — 2 local, 1 remote)

PR #112 ("feat(admin): admin UI Sub-plan 1 — auth scaffolding")
squash-merged into `main` as `6e20be9` on 2026-05-28T08:48 UTC,
landing 21 source commits + ~5,072 inserted lines under
`src/localmail/{api,serve}/admin/` and `tests/test_admin_*.py`,
plus migration `0021_api_users_is_admin.sql` and the `jinja2`
dependency. After that merge, the source branch and its operator
backup were still present locally and the source branch still
existed on the remote. All three were verified safe and pruned
this session:

```
Ref                                   Tip SHA    Disposition         Verified-via
─────────────────────────────────────────────────────────────────────────────────────────
admin-ui-sub-plan-1 (local)           dd0f18f    SQUASH-MERGED #112   gh pr view 112 → headRefOid==dd0f18f
backup/local-main-pre-pr (local)      0ddb56e    SQUASH-MERGED #112   identical tree to admin-ui-sub-plan-1
origin/admin-ui-sub-plan-1 (remote)   dd0f18f    SQUASH-MERGED #112   PR mergedAt 2026-05-28T08:48:36Z (state=MERGED)
```

- `admin-ui-sub-plan-1` (local, `dd0f18f`) — tip SHA matched exactly
  the `headRefOid` reported by `gh pr view 112 --json
  mergedAt,headRefOid,mergeCommit,state`. All 21 commits shipped via
  the squash-merge `6e20be9` on `main`. Action taken: `git branch -D
  admin-ui-sub-plan-1` (force because squash-merge is not a
  fast-forward ancestor of `main`).
- `backup/local-main-pre-pr` (local, `0ddb56e`) — operator's
  pre-merge backup merge commit; same content shipped via PR #112.
  Action taken: `git branch -D backup/local-main-pre-pr`.
- `origin/admin-ui-sub-plan-1` (remote) — squash-merged via PR #112
  but the remote branch was not auto-deleted at merge time. Action
  taken: `git push origin --delete admin-ui-sub-plan-1`. The push
  succeeded; the Dependabot reminder banner about #90 (glib) is
  unrelated and already tracked as an open issue.

No worktrees were attached to either local branch (`git worktree
list` shows only the main checkout). The prior session's local
state ended at 2 branches; PR #112 added 2 more local branches
(both pruned this session) and 1 remote branch (also pruned), so
the final state is back to 2 local branches and the minimal set of
live remotes.

### Verification

- `git branch -vv` post-prune: **2 entries** — `main` and the
  live `issue-87-…` tracking branch. Matches expected
  minimal state.
- `git ls-remote --heads origin admin-ui-sub-plan-1` post-prune
  returned empty — remote branch confirmed deleted.
- `git status` — clean (only `.claude/settings.local.json`
  untracked, by design).
- `pytest` not re-run — no source changes; the post-PR-112
  baseline on `6e20be9` is the authoritative reference (PR #112's
  CI was green at merge). The prior **830 passed, 3 warnings**
  Phase-1-pre-admin baseline at `a01b4ac` is now superseded by
  whatever PR #112 added (the 13 new `tests/test_admin_*.py` and
  `tests/test_serve_admin_*.py` modules); re-run `uv run pytest -q
  tests/` once locally if you want a current single-host count.

### Docs

- **NEXT_SESSION.md** — *replaced this session* with the current
  state (this file).
- **docs/handoffs/2026-05-28T0908-utc-post-pr-112-branch-prune.md** —
  *new this session* (this file's frozen snapshot).
- **README.md** — *unchanged*. PR #112 did not touch it (no
  user-facing CLI changes; the admin UI is reached via `serve`
  routes documented in [docs/operations/admin-ui-smoke.md]).
  Branch housekeeping has no user-facing impact either.
- **CLAUDE.md** — *unchanged*. PR #112's invariants are
  documented in the admin UI design spec ([docs/superpowers/specs/2026-05-17-localmail-gui-design.md]
  and the related sub-plan plan doc). If a Sub-plan 2 PR adds a
  new cross-cutting runtime invariant (e.g. a server-side session
  revocation column from #113), CLAUDE.md gets the one-paragraph
  summary at that time, not now.
- **ROADMAP.md** — does not exist in this repo. Not created
  (same decision as prior sessions).

## What's next

### 1. **New follow-up issues from PR #112 self-review**

PR #112 surfaced three deliberate "won't ship in Sub-plan 1"
items, all filed as open issues on 2026-05-28T08:10–08:11 UTC.
None are blockers; each is bounded scope and can ship in one PR.

- **#115** `api/auth`: promote `_DUMMY_PASSWORD_HASH` /
  `_check_login_rate_limits` / `_record_login_attempt` — admin
  code reaches into these via leading-underscore imports.
  Low-risk surface cleanup: rename to public names (or wrap in a
  thin public helper) so admin doesn't have to violate the
  private-name boundary. One PR, no behavior change. Easiest
  next session if no other driver appears.
- **#114** admin UI: `state_signing_key` required at
  `create_app()` even though Sub-plan 1 doesn't use it
  (it's a forward declaration for Sub-plan 4's OAuth web flow).
  Resolution likely deferred to Sub-plan 4, where the consumer
  lives. Document-and-defer is acceptable for now.
- **#113** admin UI: no server-side session revocation; leaked
  cookies remain valid until 8h `exp`. Deliberate Sub-plan 1
  scope choice. Likely Sub-plan 2 candidate via
  `api_users.sessions_invalidated_at` (option 1 in the issue
  body — single SELECT per request, no new table).

### 2. **Carried-forward deferred items** *(unchanged — still externally blocked)*

- **#47** `extract_worker` transient-class opt-in for third
  parties — follow-up to #36; needs production telemetry on which
  third-party extractor exceptions are recoverable before broadening
  the transient-classification list. Open until that data is
  available.
- **#90** glib Cargo alert — upstream-blocked (Tauri stack bump).
  The Dependabot banner during this session's `git push` is the
  same one.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 7** (was 4 last session; +3 from PR #112
follow-ups: #113, #114, #115).

### 3. Acceptance criteria for the most likely next concrete task

If the next session picks **#115** (lowest-cost actionable
follow-up):

- In `src/localmail/api/auth.py`, promote the three names
  (`_DUMMY_PASSWORD_HASH`, `_check_login_rate_limits`,
  `_record_login_attempt`) to public form — either rename
  (drop leading underscore) or wrap in a public helper such as
  `record_failed_login(...)`.
- Update the two callers in
  `src/localmail/api/admin/auth.py` and
  `src/localmail/serve/admin/auth_router.py` to use the public
  names; if renaming, keep the underscored aliases for one
  release (callers outside admin should not exist).
- Add or extend a focused test in
  `tests/test_api_auth.py` that proves the public surface is
  callable as documented (constant-time path on unknown user,
  rate-limit cap, audit insert).
- Acceptance: `uv run pytest -q tests/test_api_auth.py
  tests/test_admin_*.py tests/test_serve_admin_*.py` green;
  `uv run mypy src/localmail/api/auth.py
  src/localmail/api/admin/auth.py
  src/localmail/serve/admin/auth_router.py` clean; no new
  lint warnings; CLAUDE.md unchanged (this is internal-surface
  cleanup, not a new invariant).

If telemetry arrives for **#47** instead:

- Extend the transient-exception classifier in
  `src/localmail/search/extract_worker.py` to recognise the
  third-party class(es) identified by telemetry.
- Add a regression test in `tests/test_extract_worker.py` that
  proves the new class is classified as transient (not poison)
  and that the chunk is re-queued rather than landing in
  `failed_extractions`.
- Acceptance: `uv run pytest tests/test_extract_worker.py` green;
  no new lint/mypy errors; one paragraph in CLAUDE.md only if a
  new operator-facing invariant emerges.

## Open decisions & risks

1. **PR #112 follow-ups are deliberately deferred.** #113, #114,
   #115 are each marked "won't ship in Sub-plan 1" in the issue
   body. Picking any of them up is fine — they're just not
   urgent. #115 is the lowest-risk one to ship next.

2. **No orphan branches remain.** The 3 merged branches (2 local
   + 1 remote) tied to PR #112 are pruned. Future drift will
   surface through the standard `clean_gone` skill once a remote
   `[gone]` marker appears.

3. **`.claude/settings.local.json` stays untracked.** Same as
   prior sessions — local-only file.

4. **CLAUDE.md not updated for PR #112.** Deliberate: the admin
   UI design spec and sub-plan doc already capture the
   architectural decisions, and PR #112 introduces no
   cross-cutting runtime invariant that would surprise a future
   reader of the rest of CLAUDE.md. Revisit if Sub-plan 2 ships
   a server-side session-revocation column (#113).

5. **No code changes this session.** Same outcome as the prior
   four sessions when no source-tree work was in scope. With
   branch housekeeping now exhausted again, the next session
   has an obvious actionable starter (#115) if no other driver
   appears.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect clean working tree (only .claude/* untracked)
git log --oneline -3                 # tip on main:
                                     #   <new>     docs(handoffs): land 2026-05-28T0908 UTC post-PR-112 branch prune snapshot
                                     #   6e20be9   feat(admin): admin UI Sub-plan 1 — auth scaffolding (#112)
                                     #   b95a105   docs(handoffs): land 2026-05-27T2202 UTC orphan-branch-prune snapshot
git branch -vv                       # expect 2 entries (main + issue-87-…)
gh issue list --state open --limit 40   # expect 7 open (#47, #90, #25, #5, #113, #114, #115)

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (now includes the new admin tests)
unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_*.py tests/test_serve_admin_*.py   # admin slice
unset VIRTUAL_ENV && uv run mypy src/localmail/api/admin src/localmail/serve/admin
cd gui && npm test -- --run                         # GUI client (last verified: 312 passed)
cd gui && npm run check                             # GUI typecheck (last verified: 0 errors)
cd gui/src-tauri && cargo test                      # GUI Rust (last verified: 79 passed)
```

If picking up **#115** specifically:

```bash
git checkout -b issue-115-promote-auth-helpers
# edit src/localmail/api/auth.py + src/localmail/api/admin/auth.py +
#      src/localmail/serve/admin/auth_router.py + tests/test_api_auth.py
unset VIRTUAL_ENV && uv run pytest -q tests/test_api_auth.py tests/test_admin_*.py tests/test_serve_admin_*.py
unset VIRTUAL_ENV && uv run mypy src/localmail
```

## File map (post-session)

```
NEXT_SESSION.md                                                 # REPLACED this session
docs/handoffs/
  2026-05-28T0908-utc-post-pr-112-branch-prune.md               # NEW (this session's snapshot)
  2026-05-27T2202-utc-orphan-branch-prune.md                    # prior
  2026-05-27T1438-utc-stale-branch-prune.md                     # earlier
  2026-05-27T1427-utc-pr-111-followup-verified.md               # earlier
  2026-05-27T1409-utc-gui-stale-comments-pr-111.md              # earlier
  2026-05-27T1352-utc-changes-tail-only-pr-110.md               # earlier
  2026-05-27T1336-utc-exit-code-spec-alignment-pr-109.md        # earlier
  2026-05-27T1319-utc-chunks-gin-projection-pr-108.md           # earlier
  2026-05-27T1248-utc-upgrade-estimator-pr-102.md               # earlier
  …
```

`main` is at `<new>` after this session's admin commit. Working
tree clean (only `.claude/settings.local.json` untracked, by
design). 2 local + 1 remote branches deleted (PR #112 source +
backup + remote); 2 local branches remain (`main`, `issue-87-…`).
