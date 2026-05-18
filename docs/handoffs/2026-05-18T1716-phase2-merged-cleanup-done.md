# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-18 evening (local).** PR #30 is **merged**
> (`e2b6a10`). `main` now carries: sync daemon + Phase 1 hybrid search +
> Phase 2 attachment search + GUI HTTPS server + GUI Tauri/Svelte client.
> Test suite green (401 passed, 0 failed). Stale feature branches and
> worktrees pruned locally. Next session's job: pick the highest-signal
> follow-up issue and ship it.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. The archive is consumed by
downstream agents directly (DB + attachment tree) and/or via the
`localmail serve` HTTPS API + Tauri/Svelte desktop client.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

The phase2-hybrid-search → main integration landed and the local repo got
swept clean.

| SHA / artifact | What |
|---|---|
| `e2b6a10` | **PR #30 merged** — feat: GUI HTTPS server + Phase 2 search (attachments, filters). 83 files, +10,400 / −135. |
| (this session) | `README.md` rewritten — added GUI server section (`localmail serve`, `add-api-user`, `rotate-tls`), GUI client section pointing at [gui/](gui/), and a full CLI table split into Sync, Search-backfill, and GUI-server groupings. Phase-1 Search section renamed to cover Phase 1 + Phase 2 (attachment text + `extract-backfill`). |
| (this session) | Worktree cleanup — removed all 7 stale worktrees under `.claude/worktrees/` (phase1, phase2, ci-secret-service-fix, gui-client-2/3/4/5). Deleted the corresponding 8 local branches (`-d` for true merges; `-D` only after `gh pr list --state merged --head BRANCH` confirmed the squash-merged ones via PRs #26, #29, #30). |
| (this session) | Verified `unset VIRTUAL_ENV && uv run pytest -q` → **401 passed, 0 failed** in 52.59s on `main` post-merge. |

GitHub issue **#11** auto-closed when PR #30 merged (account/folder/date/
lang filter forwarding shipped).

## What's next — concrete acceptance criteria

PR #30 surfaced several follow-up issues. Pick the one with the most
leverage and ship it as a focused PR. The top candidates, ranked by
"unblocks user-visible behaviour":

### 1. Populate `messages.body_lang` (issue #34) — unblocks the `lang:` DSL token

Migration 0015 added the column and the `lang:` query token + API filter
plumbing landed in PR #30, but **no worker writes to `body_lang`**, so
`lang:en` currently returns 0 results.

Acceptance:
- Embed worker detects language per message (e.g. `lingua-py`, `langdetect`,
  or a fastembed-compatible model) and writes the result to
  `messages.body_lang` during the same sweep that produces the chunks.
- ISO 639-1 lowercase codes; NULL when detection confidence is below a
  configurable threshold (`SearchConfig.body_lang_min_confidence`).
- A one-shot backfill path (either a CLI subcommand, e.g.
  `localmail backfill-body-lang`, or a reuse of `embed-backfill` once the
  column is wired into chunking) populates the existing archive.
- New unit tests cover detection on de/en/es/ja/no fixtures from
  `tests/_multilingual_corpus.py`.
- Acceptance harness in `tests/acceptance/run_recall_eval.py` exercises
  `lang:` filters and confirms non-zero recall per language.

### 2. Per-user account ACL (issue #31) + thread `AuthenticatedUser` through every route

Today, every `localmail serve` API user has read access to every account.
PR #30 documents this in `add-api-user --help`. To remove the warning:

- New table `user_accounts (user_id, account_id)` via migration `0016_*`.
- Every route currently receiving the global session reads the
  `AuthenticatedUser` from the request state and filters by that user's
  allowed account IDs at the SQL boundary.
- New tests: `tests/test_serve_*` get a "alice can see acct A but not B"
  case.
- CLI: `localmail grant-account USERNAME ACCOUNT_NAME` / `revoke-account`.
- Auto-closes issues #31 and #8 together.

### 3. Migrate HTML sanitisation from `bleach` to `nh3` (issue #13)

`bleach[css]>=6.2` was an interim hardening in PR #30. `nh3` (ammonia
bindings) is faster, maintained, and the long-term direction. Drop-in for
`bleach.clean` is mostly mechanical but the allowlist surface differs;
golden-file tests in `tests/test_sanitize_*.py` catch regressions.

### 4. Distinguish transient vs poison-pill in `extract_worker` (issue #36)

Mirrors the issue called out in CLAUDE.md for `embed_worker`: today a
network blip during a `docling` model download can mark a perfectly fine
PDF as permanently failed. Need to scope the "permanent failure" classes
narrowly (parser raises, MIME type wrong, etc.) and let everything else
back off and retry.

### Other open issues worth keeping on the radar

- **#35** RRF k may need re-tuning now that arm 4 (attachment chunks) is in the fusion.
- **#32** Attachment streaming — Range support, Content-Disposition, MIME hardening.
- **#33** Unify ID typing across endpoints (strings on the wire, ints in DB).
- **#37** Unify ConnectionPool ceiling across IDLE/poll/embed/extract workers (+ closes #9).
- **#38** `/v1/changes` semantics — initial backfill window decision.
- **#27 / #28 / #22 / #24 / #18 / #17** GUI client polish & CI.
- **#25** uvicorn / websockets.legacy deprecation (loud warning during tests).
- **#10 / #12** Persist Content-ID on attachments (inline `cid:` rendering).
- **#7** IP-based / global login rate limiter.

## Open decisions & risks

1. **Stale remote branches.** Local branches were deleted but
   `origin/ci-secret-service-fix`, `origin/gui-client-1`,
   `origin/gui-client-4`, `origin/gui-client-5`, `origin/gui-server`,
   `origin/worktree-phase1-hybrid-search`, and
   `origin/worktree-phase2-hybrid-search` still exist on GitHub.
   Push-delete (`git push origin --delete BRANCH`) is the next step; left
   for the maintainer to do via the web UI or a deliberate batch, since
   it's a destructive remote action.

2. **`websockets.legacy` DeprecationWarning** (issue #25) still fires during
   `test_e2e_serve.py`. Pre-existing — PR #30 didn't touch it. Tracked.

3. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) surfaced
   during the merge push. Now that the dep tree is unified, run a triage
   pass before tackling new features: `gh api repos/hherb/localmail/
   vulnerability-alerts` or the GitHub Security tab.

4. **`messages.body_lang` is intentionally NULL.** See "What's next #1"
   above. Plumbing shipped so the backfill can be a separate, narrower PR.

5. **No ROADMAP.md.** Per-session NEXT_SESSION + open issues currently
   serve as the roadmap. If the project grows beyond ~50 open issues a
   real ROADMAP.md may be worth introducing; today it would just duplicate
   GitHub Issues + this file.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git checkout main && git pull --ff-only

# Smoke the merged tree.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q          # expect 401 passed
unset VIRTUAL_ENV && uv run localmail init-db  # idempotent
unset VIRTUAL_ENV && uv run localmail search-status

# Review open issues and pick a target.
gh issue list --state open --limit 40

# Start a feature branch for #34 (recommended next):
git checkout -b feat/body-lang-detection
# … TDD: write the failing detection tests first, then wire into embed_worker.
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active` will
  pick the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB. The conftest enforces this
  but the env var still has to be reachable for DB tests to run; otherwise
  they skip.
- **Migrations 0011–0015 are additive.** Re-running `init-db` on a
  Phase-1 archive is safe (idempotent), but back up first if the archive
  is non-trivial.
- **`docling` is the Phase 2 extractor.** Install with
  `uv sync --extra extraction` if you need attachment-text extraction
  locally — it pulls a large model graph and is opt-in for that reason.
- **TLS for `localmail serve`** — `--bind 0.0.0.0` requires `--tls-cert`
  + `--tls-key`. `--no-tls` is only honoured on `127.0.0.1`. Use
  `localmail rotate-tls` to generate a self-signed cert.

## File map (post-PR-#30, current state on `main`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py attachments.py auth.py errors.py messages.py
    sanitize.py search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/                          # auth, accounts, messages, attachments,
                                     # changes, search, version
  search/
    arms.py chunking.py embed_worker.py embeddings.py extractor.py
    extract_worker.py page_cache.py query.py reranker.py searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0015
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of merge-day session. Repo is clean; pick a follow-up issue and ship.
