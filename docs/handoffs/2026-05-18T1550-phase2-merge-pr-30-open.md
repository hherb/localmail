# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-18 evening (local).** The phase2 → main merge
> is **open as PR #30**: <https://github.com/hherb/localmail/pull/30>.
> Diff is clean (83 files, +10,400 / -135 — no apparent GUI deletions).
> Next session's job: review PR #30, smoke the merged tree, merge.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. After PR #30 merges,
`main` will carry: sync daemon + Phase 1 hybrid search + Phase 2
attachment search + GUI HTTPS server + GUI Tauri/Svelte client. See
[CLAUDE.md](CLAUDE.md) and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What's done (cumulative)

| Component | Status |
|---|---|
| **Sub-plans 1–5** GUI client | ✅ on `main` (PR #26 + earlier) |
| **CI: Linux secret-service** | ✅ PR #29 → `main` |
| **GUI server + Phase 2 search** | 🟡 **PR #30 open** (`worktree-phase2-hybrid-search → main`) |

## What this session shipped

This session reconciled `worktree-phase2-hybrid-search` (79 commits of
GUI server + Phase 2 search) with the rapidly-evolving `main`
(GUI client merged via 6 PRs since the branch diverged), then opened
the merge PR.

### On `worktree-phase2-hybrid-search`

| SHA | What |
|---|---|
| `fdf9680` | **Merge commit** — `Merge branch 'main' into worktree-phase2-hybrid-search`. Brings in the GUI client, skill files, handoffs, GUI plans/spec, banner assets, `gui-ci.yml`, broader `.gitignore`. Without this, the eventual `phase2 → main` PR would have shown deleting 195 GUI-side files. |

#### Conflict resolutions inside `fdf9680`

- **pyproject.toml** — union of both branches' deps. Phase 2 contributed
  `fastapi>=0.115`, `uvicorn[standard]>=0.32`, `argon2-cffi>=23.1`,
  `bleach[css]>=6.2`, `cryptography>=43.0`, and the
  `[project.optional-dependencies] extraction = ["docling>=2.20"]` extra.
  Main contributed `sqlparse>=0.5`. All kept. **Bonus fix:** `sqlparse`
  is already required by `db.py:_split_statements` on phase2 but had
  never been declared on that branch — the union now correctly declares
  it.
- **uv.lock** — regenerated via `uv lock` rather than hand-merged.
  Resolved to 179 packages.
- **CLAUDE.md**, **`.gitignore`**, **`src/localmail/config.py`** —
  auto-merged cleanly (no conflict markers).

#### Verification on `fdf9680`

- `unset VIRTUAL_ENV && uv run pytest -q` → **390 passed, 0 failed**
  in 18.64s. (Was 371 on phase2 HEAD; +19 are the main-side tests now
  reachable inside the merged tree — locale-fix, line-clamp test, GUI
  store tests added during Sub-plans.)
- `git diff --diff-filter=D --name-only origin/main..HEAD | wc -l`
  → 0 (was 195 before merge).
- `git rev-list --left-right --count origin/main...HEAD` → `0  80`
  (main fully present in phase2; 80 phase2-only commits incl. the
  merge commit itself).

### PR opened

- **PR #30** — <https://github.com/hherb/localmail/pull/30>
  - Title: `feat: GUI HTTPS server + Phase 2 search (attachments, filters)`
  - 83 files changed, +10,400 / -135.
  - Test plan + open decisions + known follow-ups documented in the
    PR body.

## What's next — concrete acceptance criteria

The PR is open; the next session's job is to land it.

### 1. PR #30 review pass

- Walk `gh pr view 30 --web` (or `git diff origin/main..origin/worktree-phase2-hybrid-search`).
- Spot-check the new surface:
  - `src/localmail/api/*` — service layer (transport-free).
  - `src/localmail/serve/*` — FastAPI wrapper, routes.
  - `src/localmail/search/extractor.py` + `extract_worker.py`.
  - `migrations/0011…0015`.
- Apply the "fixall" / "code-review" skills if useful.
- Address any reviewer feedback via additional commits on
  `worktree-phase2-hybrid-search` (the branch can keep moving until
  the merge happens).

### 2. Post-merge smoke

After PR #30 merges to main:

```bash
cd /Users/hherb/src/localmail
git checkout main && git pull
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q          # expect 390 passed
unset VIRTUAL_ENV && uv run localmail init-db  # applies 0011…0015 on the live DB
unset VIRTUAL_ENV && uv run localmail serve --bind 127.0.0.1 --port 8443 \
                                            --tls-cert PATH --tls-key PATH
# In another terminal, run the GUI client:
cd gui && npm run tauri dev
# Click through: connect → login → search → open message → attachments.
```

Acceptance:
- Migrations 0011–0015 apply cleanly to an existing Phase-1 archive
  (no data loss, additive only).
- `/v1/health`, `/v1/capabilities`, `/v1/version` return 200 over TLS.
- GUI client successfully connects + searches + reads messages +
  fetches attachments.
- GitHub issue **#11** auto-closes once `account_ids/folder_ids/
  date_from/date_to/lang` filter forwarding lands on main.

### 3. Worktree cleanup (post-merge only)

```bash
cd /Users/hherb/src/localmail
# Safe to remove — all merged:
git worktree remove .claude/worktrees/phase2-hybrid-search
git worktree remove .claude/worktrees/phase1-hybrid-search
git worktree remove .claude/worktrees/ci-secret-service-fix
git worktree remove .claude/worktrees/gui-client-2
git worktree remove .claude/worktrees/gui-client-3
git worktree remove .claude/worktrees/gui-client-4
git worktree remove .claude/worktrees/gui-client-5
# Then drop the merged branches:
git branch -d worktree-phase1-hybrid-search ci-secret-service-fix \
              gui-client-2 gui-client-3 gui-client-4 gui-client-5 fixissues
# (Do NOT delete worktree-phase2-hybrid-search until PR #30 is merged.)
```

### 4. README update (post-merge)

The README on main still has zero `gui` / `serve` references because
both lived on feature branches. After PR #30 merges, add:
- A "GUI client" install/run section.
- A "GUI server" section covering `localmail serve`, TLS,
  `add-api-user`, `change-password` route.
- The five new CLI commands (`serve`, `add-api-user`,
  `list-api-users`, `remove-api-user`, `rotate-tls`).
- The four extraction commands (`extract-backfill`,
  `list-failed-extractions`, `retry-failed-extractions`, and the
  expanded `search-status`).

## Open decisions & risks

1. **`messages.body_lang` population is deferred.** Migration 0015
   adds the column and the `lang:` DSL token + API filter forwarding
   work end-to-end, but no embed-worker change writes to `body_lang`.
   The filter therefore returns 0 results until either (a) the
   embed worker detects language per-message during chunking, or
   (b) a one-shot backfill script runs. Plumbing intentionally
   shipped now so the backfill can be a separate, narrower PR.

2. **CLAUDE.md says "Latest is `0013_attachment_search_indexes.sql`".**
   Stale — actual latest after PR #30 will be `0015_messages_body_lang.sql`.
   Pre-existing on phase2 (the change-password + body_lang migrations
   were added without updating the conventions section). Fix in a
   tiny follow-up commit; surfaced in the PR body so reviewers know.

3. **GitHub Dependabot shows 12 vulnerabilities** (1 high / 9 mod / 2 low)
   on `main`'s dep tree (surfaced during `git push`). Worth a triage
   pass after PR #30 merges so the alerts are evaluated against the
   union dep set, not the pre-merge slice.

4. **`websockets.legacy` DeprecationWarning** (open issue #25)
   surfaces during `test_e2e_serve.py`. Pre-existing on phase2 —
   PR #30 doesn't fix it. Tracked.

5. **Open follow-up issues** (highest signal first, after PR #30):
   - **#11** auto-closes on PR #30 merge (filters wired).
   - **#13** Migrate HTML sanitisation from bleach to nh3 (the CSS
     sanitizer wired in `8002038` was an interim hardening).
   - **#10 / #12** Persist Content-ID on attachments (inline `cid:`
     image rendering).
   - **#28 / #27 / #24 / #22** GUI client cleanups.
   - **#25** uvicorn / websockets.legacy deprecation.
   - **#7 / #8 / #9** Auth hardening (per-user ACL, IP rate limit,
     pool sizing).

## Exact commands to resume

```bash
# Review PR #30:
cd /Users/hherb/src/localmail
gh pr view 30
gh pr view 30 --web                            # opens in browser
gh pr diff 30 --color=always | less -R         # full diff

# Iterate on the PR (if reviewer asks for changes):
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
# … make edits, run tests, commit, push
unset VIRTUAL_ENV && uv run pytest -q
git push origin worktree-phase2-hybrid-search

# Merge PR #30 (via gh):
gh pr merge 30 --merge   # repo convention is merge commits, not squash

# After merge: smoke on main
cd /Users/hherb/src/localmail
git checkout main && git pull
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q
unset VIRTUAL_ENV && uv run localmail init-db
unset VIRTUAL_ENV && uv run localmail serve --bind 127.0.0.1 --port 8443 \
                                            --tls-cert PATH --tls-key PATH
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  will pick the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB. The conftest enforces
  this but the env var still has to be reachable for DB tests to run;
  otherwise they skip.
- **`uv lock` after pyproject.toml edits.** The Phase 2 + GUI server
  branches both touch deps; if you edit pyproject.toml on any
  follow-up, regenerate uv.lock with `uv lock` (not hand-edit).
- **Migrations 0011–0015 are additive.** Re-running `init-db` on a
  Phase-1 archive should be safe (idempotent), but back up first if
  the archive is non-trivial.
- **`sqlparse` is now a hard dependency** (was implicit before via
  some transitive route). Pinned at `>=0.5`. Required by `db.py:
  _split_statements` for migration-runner correctness — see the
  `_split_statements` note in CLAUDE.md.

## File map (post-PR-#30, expected state on `main`)

```
src/localmail/
  api/                               # transport-free service library (Phase 2)
  serve/                             # FastAPI wrapper + TLS + middleware
    routes/                          # auth, accounts, messages, attachments, changes, search, version
  search/
    extractor.py + extract_worker.py # Phase 2 attachment-text extraction
gui/                                 # Tauri 2 + Svelte 5 client (Sub-plans 1–5)
migrations/                          # 0001 … 0015
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of merge-prep session. PR #30 is the next session's primary
focus. Local merge worktree (`.claude/worktrees/phase2-hybrid-search`)
is ahead of origin by zero — push completed.
