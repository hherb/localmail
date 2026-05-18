# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-18 evening (local).** PR #39 is **merged**
> (`a0bda62` on `main`). The squashed merge bundles the original
> `messages.body_lang` worker (`1f34e05`) together with the post-review
> drain/return-count fix (`ba0748e`), so `lang-backfill` and the
> `lang:` DSL token are both correct on `main`. Test suite green
> (427 passed, 0 failed). Issue #34 closed. Next session's job: pick the
> highest-leverage open issue and ship it.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. The archive is consumed by
downstream agents directly (DB + attachment tree) and/or via the
`localmail serve` HTTPS API + Tauri/Svelte desktop client.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

The body_lang follow-up + post-merge cleanup landed.

| SHA / artifact | What |
|---|---|
| `a0bda62` | **PR #39 merged** — feat(search): populate `messages.body_lang` via lingua-py (closes #34). Squash-merge bundles the original feature + the drain/return-count fix. 14 files, +853 / −41. |
| `1f34e05` (on feature branch, squashed away) | Original feature commit: `localmail.search.lang_detect` (FixedDetector + LinguaDetector + `run_lang_detect_pass`), embed-worker integration, `lang-backfill` CLI, `search-status` lang counters. |
| `ba0748e` (on feature branch, squashed away) | Post-merge fix landed before squash: `run_lang_detect_pass` now returns NULL→non-NULL transitions (not claimed rows) so loops terminate when bodies are persistently below the length/confidence floors; `embed-backfill` explicitly drains the lang queue after embeddings finish. |
| (this session) | Verified `unset VIRTUAL_ENV && uv run pytest -q` → **427 passed, 0 failed** in 19.23s on `main`. |
| (this session) | Cherry-pick check confirmed `feat/body-lang-detection` has no unique code on top of `main` — both commits are in the squash. The branch is safe to delete. |

New deferred follow-up surfaced during PR #39 review: **issue #40** — add
partial index `messages(id) WHERE body_lang IS NULL` for the lang-detect
worker. Not blocking; tracked.

## What's next — concrete acceptance criteria

Pick the issue with the most leverage and ship it as a focused PR. Ranked
by "unblocks user-visible behaviour":

### 1. Per-user account ACL — issues #31 (api) + #8 (auth)

Today every `localmail serve` API user has read access to every account.
PR #30 documents this in `add-api-user --help`. To remove the warning:

- New migration `0016_user_accounts.sql`: `user_accounts (user_id, account_id)` join.
- Every route that currently receives the global session reads the
  `AuthenticatedUser` from request state and filters by allowed account
  IDs at the SQL boundary (messages, attachments, changes, search,
  accounts list).
- New CLI: `localmail grant-account USERNAME ACCOUNT_NAME` /
  `revoke-account USERNAME ACCOUNT_NAME`.
- New tests in `tests/test_serve_*.py`: "alice can see acct A but not B"
  covering messages, attachments, search filters, `/v1/changes`.
- Auto-closes **#31** and **#8** together.

### 2. Migrate HTML sanitisation from `bleach` to `nh3` (issue #13)

`bleach[css]>=6.2` was an interim hardening in PR #30. `nh3` (ammonia
bindings) is faster, maintained, and the long-term direction. Drop-in
for `bleach.clean` is mostly mechanical; the allowlist surface differs.
`tests/test_sanitize_*.py` golden files catch regressions.

### 3. Distinguish transient vs poison-pill in `extract_worker` (issue #36)

Mirrors the policy `embed_worker` already enforces: today a network blip
during a `docling` model download can mark a perfectly fine PDF as
permanently failed. Scope "permanent failure" classes narrowly (parser
raises, MIME mismatch, etc.) and let everything else back off and retry.
No new `failed_attachment_chunkings` table needed — surface persistent
failures via WARNING logs as today.

### 4. RRF `k` re-tuning (issue #35)

Now that arm 4 (attachment chunks) is in the RRF fusion, the default
`rrf_k = 60` may over-weight large pools. Acceptance harness already
measures recall + MRR — sweep `k ∈ {30, 45, 60, 90}` and pick the best.

### Other open issues on the radar

- **#40** Partial index `WHERE body_lang IS NULL` for the lang-detect
  worker — not blocking until archives exceed ~100k messages.
- **#37** Unify ConnectionPool ceiling across IDLE/poll/embed/extract
  workers (+ closes #9).
- **#38** `/v1/changes` semantics — initial backfill window decision.
- **#32** Attachment streaming — Range support, Content-Disposition,
  MIME hardening.
- **#33** Unify ID typing across endpoints (strings on the wire, ints
  in DB).
- **#27 / #28 / #22 / #24 / #18 / #17** GUI client polish & CI.
- **#25** uvicorn / websockets.legacy deprecation (loud warning during
  tests; still firing in `test_e2e_serve.py`).
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`
  rendering).
- **#7** IP-based / global login rate limiter.
- **#5** Batch INSERT for chunking loop (perf follow-up).
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#3** `db._split_statements` is now `sqlparse`-backed; issue may be
  stale — verify before closing.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.

## Open decisions & risks

1. **Stale local & remote feature branches.** `feat/body-lang-detection`
   is fully squash-merged but still exists locally (`git branch -d`
   safe) and on `origin/`. Push-delete is left for the maintainer to
   batch with the other stale remotes from the previous session
   (`ci-secret-service-fix`, `gui-client-1/4/5`, `gui-server`,
   `worktree-phase1-hybrid-search`, `worktree-phase2-hybrid-search`).

2. **`websockets.legacy` DeprecationWarning** (issue #25) still fires
   during `test_e2e_serve.py`. Pre-existing — PR #39 didn't touch it.
   Tracked.

3. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low)
   surfaced during PR #30's merge push and were carried through #39.
   Run a triage pass before tackling new features: `gh api
   repos/hherb/localmail/vulnerability-alerts` or the GitHub Security
   tab.

4. **`body_lang` partial index deferred** (issue #40). Worker query
   `WHERE body_lang IS NULL AND body_text IS NOT NULL` currently
   seq-scans because migration 0015's partial index is on the inverse
   condition. Acceptable until archives hit ~100k messages.

5. **No ROADMAP.md.** Per-session NEXT_SESSION + open issues continue
   to serve as the roadmap. Reconsider only if open issues exceed ~50.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git checkout main && git pull --ff-only

# Smoke the merged tree.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q              # expect 427 passed
unset VIRTUAL_ENV && uv run localmail init-db      # idempotent
unset VIRTUAL_ENV && uv run localmail search-status

# Clean up the just-merged feature branch (optional, local only):
git branch -d feat/body-lang-detection

# Review open issues and pick a target.
gh issue list --state open --limit 40

# Start a feature branch for #31/#8 (recommended next):
git checkout -b feat/per-user-account-acl
# ... TDD: write the failing "alice can see A but not B" tests first,
# then thread AuthenticatedUser through the routes.
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  will pick the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB. The conftest enforces
  this but the env var still has to be reachable for DB tests to run;
  otherwise they skip.
- **Migrations 0011–0015 are additive.** Re-running `init-db` on an
  older archive is safe (idempotent), but back up first if the archive
  is non-trivial.
- **`docling` is the Phase 2 extractor.** Install with
  `uv sync --extra extraction` if you need attachment-text extraction
  locally — it pulls a large model graph and is opt-in for that reason.
- **TLS for `localmail serve`** — `--bind 0.0.0.0` requires `--tls-cert`
  + `--tls-key`. `--no-tls` is only honoured on `127.0.0.1`. Use
  `localmail rotate-tls` to generate a self-signed cert.
- **First-time `body_lang` install** — run `localmail lang-backfill`
  (or `embed-backfill`, which drains both queues) once after upgrade so
  `lang:` queries return rows.

## File map (post-PR-#39, current state on `main`)

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
    extract_worker.py lang_detect.py page_cache.py query.py reranker.py
    searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0015
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of body_lang follow-up session. Repo is clean; pick a follow-up
issue and ship.
