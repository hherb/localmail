# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-27T2202 UTC.**
> **No source changes this session — local branch housekeeping only.**
> The two upstream-less orphan branches noted in the prior handoff
> (`fix/sanitize-test-coverage` @ `d9c7250`, `pr-69-review` @ `9b6baee`)
> were deleted. `main` is unchanged at `407054b` from the prior session
> until this session's admin commit lands. CI on `origin/main` is still
> green from the PR #111 merge chain.
>
> Local branches now reduced to **2** (was 4):
>
> - `main` — at `407054b` (about to advance with this session's
>   handoff commit).
> - `issue-87-at-scale-folder-filter-regression-coverage` — tracks
>   live `origin/issue-87-…`; still active on the remote, kept.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
the DB + attachment tree directly or via the `localmail serve` HTTPS API.
The Tauri 2 + Svelte 5 desktop GUI lives at [gui/](gui/).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md),
[docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md),
and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

### Issue + PR

- **No new issue, no new PR.** Session was local-only branch
  housekeeping (touches only `NEXT_SESSION.md` + this session's
  handoff snapshot under `docs/handoffs/`).

### Commits (1 — handoff/admin only)

```
<filled in after `git commit`>  docs(handoffs): land 2026-05-27T2202 UTC orphan-branch-prune snapshot
```

The single commit lands this updated `NEXT_SESSION.md` plus its
frozen archive under `docs/handoffs/`. No source-tree changes.

### Branch housekeeping (2 orphan local branches deleted)

The prior 2026-05-27T1438 session explicitly left two upstream-less
local branches in place because they fell outside the `[gone]`
clean-up skill's scope (they never tracked an upstream, so
`git fetch --prune` cannot mark them `[gone]`). The operator
opted to prune them this session for a fully minimal `git branch`
list.

Each was verified safe before deletion:

```
Branch                          Tip SHA   Reachable from main?  Content shipped via
─────────────────────────────────────────────────────────────────────────────────────
fix/sanitize-test-coverage      d9c7250   YES (ancestor)         PR #44 (closes #43)
pr-69-review                    9b6baee   no                     PR #69 (closes #7)
```

- `fix/sanitize-test-coverage` (`d9c7250`) — `git merge-base
  --is-ancestor d9c7250 main` returns 0, i.e. the commit is
  literally an ancestor of `main`. Note this **corrects** the
  prior handoff's claim that this branch was squash-merged —
  it wasn't; the SHA matches between branch tip and main. The
  branch was deletable with `git branch -d` (non-force), not
  `-D`. Action taken: `git branch -d fix/sanitize-test-coverage`.

- `pr-69-review` (`9b6baee`) — not an ancestor of main, but the
  handoff doc it added
  (`docs/handoffs/2026-05-20T2200-postgres-login-rate-limiter-pr-69.md`)
  exists in the working tree on main, and NEXT_SESSION.md has
  been rewritten many times since. The branch's content
  effectively shipped via PR #69 (squash-merged as `ed21237`
  "feat(auth): Postgres-backed login rate limiter (closes #7)
  (#69)"). Action taken: `git branch -D pr-69-review`.

No worktrees were attached to either branch (`git worktree list`
shows only the main checkout).

### Verification

- `git branch -vv` post-prune: **2 entries** — `main` and the
  live `issue-87-…` tracking branch. Matches expected
  minimal state.
- `git status` — clean (only `.claude/settings.local.json`
  untracked, by design).
- `pytest` not re-run — no source changes; prior session's
  **830 passed, 3 warnings in 39.30s** baseline at `a01b4ac`
  still holds.

### Docs

- **NEXT_SESSION.md** — *replaced this session* with the current
  state (this file).
- **docs/handoffs/2026-05-27T2202-utc-orphan-branch-prune.md** —
  *new this session* (this file's frozen snapshot).
- **README.md** — *unchanged*. Branch housekeeping has no
  user-facing impact.
- **CLAUDE.md** — *unchanged*. No new architecture or runtime
  invariants from this session.
- **ROADMAP.md** — does not exist in this repo. Not created
  (same decision as prior sessions).

## What's next

### 1. **Carried-forward deferred items** *(unchanged — still externally blocked)*

- **#47** `extract_worker` transient-class opt-in for third
  parties — follow-up to #36; needs production telemetry on which
  third-party extractor exceptions are recoverable before broadening
  the transient-classification list. Open until that data is
  available.
- **#90** glib Cargo alert — upstream-blocked (Tauri stack bump).
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 4** (unchanged — this session opens no new
issues and closes none).

### 2. **Possible next sessions** *(no urgent driver)*

Branch state is now fully minimal — no further housekeeping is
available. All open issues remain blocked on inputs from outside
this repo. Productive options:

- **Wait** for input on #47, #5 — no work to do here without it.
- **Net-new operator QoL**: small `localmail search-status` or
  `estimate-upgrade` augmentations would fit a short session if the
  operator has a concrete ask.
- **Cross-cutting follow-up audits**: speculative — only worth
  pursuing if triggered by a specific reader-confusion report.

### 3. Acceptance criteria for the most likely next concrete task

If/when telemetry arrives for #47 (the most likely next driver):

- Extend the transient-exception classifier in
  `src/localmail/search/extract_worker.py` to recognise the
  third-party class(es) identified by telemetry.
- Add a regression test in `tests/test_extract_worker.py` that
  proves the new class is classified as transient (not poison)
  and that the chunk is re-queued rather than landing in
  `failed_extractions`.
- Update [docs/superpowers/specs/2026-05-16-hybrid-search-phase2-design.md](docs/superpowers/specs/2026-05-16-hybrid-search-phase2-design.md)
  if the classifier surface or behaviour changes materially.
- Acceptance: `uv run pytest tests/test_extract_worker.py` green;
  no new lint/mypy errors; one paragraph in CLAUDE.md only if a
  new operator-facing invariant emerges.

## Open decisions & risks

1. **No orphan branches remain** — the residual concerns about
   `fix/sanitize-test-coverage` / `pr-69-review` from the prior
   handoff are resolved. Future drift will surface through the
   standard `clean_gone` skill once a remote `[gone]` marker
   appears.

2. **`.claude/settings.local.json` stays untracked.** Same as
   prior sessions — local-only file.

3. **No code changes this session.** Same outcome as the prior
   three sessions for the same reason: all open issues are
   blocked on external inputs. With branch housekeeping now
   exhausted, the next session has no available "fill" task —
   either input arrives on an open issue, or the operator picks
   a concrete net-new ask.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect clean working tree (only .claude/* untracked)
git log --oneline -3                 # tip on main:
                                     #   <new>     docs(handoffs): land 2026-05-27T2202 UTC orphan-branch-prune snapshot
                                     #   407054b   docs(handoffs): land 2026-05-27T1438 UTC stale-branch-prune snapshot
                                     #   a01b4ac   docs(handoffs): land 2026-05-27T1409 + 1427 UTC handoff snapshots
git branch -vv                       # expect 2 entries (main + issue-87-…)
gh issue list --state open --limit 40   # expect 4 open (#47, #90, #25, #5)

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/       # 830 passed baseline
cd gui && npm test -- --run                        # 312 passed (last verified)
cd gui && npm run check                            # 0 errors, 0 warnings
cd gui/src-tauri && cargo test                     # 79 passed (last verified)
```

## File map (post-session)

```
NEXT_SESSION.md                                                 # REPLACED this session
docs/handoffs/
  2026-05-27T2202-utc-orphan-branch-prune.md                    # NEW (this session's snapshot)
  2026-05-27T1438-utc-stale-branch-prune.md                     # prior session
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
design). 2 orphan local branches deleted; 2 local branches remain
(`main`, `issue-87-…`).
