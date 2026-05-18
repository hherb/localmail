# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-18 evening (local).** PR #39 (`1f34e05`) is
> **open** on `feat/body-lang-detection` and ready to merge: it wires the
> embed worker + a new `lang-backfill` CLI to populate `messages.body_lang`
> via `lingua-language-detector`, so the `lang:` DSL token finally returns
> rows. Test suite green (426 passed, +25 from baseline). Next session's
> job: merge #39, then pick the next-highest-leverage follow-up.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. The archive is consumed by
downstream agents directly (DB + attachment tree) and/or via the
`localmail serve` HTTPS API + Tauri/Svelte desktop client.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

Issue #34 (body_lang population) — the deferred half of PR #30's `lang:`
plumbing — is now ready to land.

| SHA / artifact | What |
|---|---|
| `1f34e05` on `feat/body-lang-detection` (PR #39) | **feat(search): populate messages.body_lang via lingua-py.** New `localmail.search.lang_detect` (FixedDetector / LinguaDetector / `run_lang_detect_pass`); embed_worker accepts optional `lang_detector` kwarg; daemon + `embed-backfill` instantiate it via `make_detector(cfg.search)`; new `localmail lang-backfill` CLI; `search-status` reports `body_lang_populated` / `body_lang_pending`; acceptance harness exercises the pass. Five new `SearchConfig` knobs, no magic numbers. README + CLAUDE.md refreshed. |
| (this session) | Verified `unset VIRTUAL_ENV && uv run pytest -q` → **426 passed, 0 failed** (+25 tests). Mypy clean on touched files. Standalone smoke against the synthetic 50-message corpus: 10/10 de, 9/10 en, 8/10 es, 9/10 ja; Norwegian returns `nb`/`nn` (lingua doesn't map to the umbrella `no`, matching the existing acceptance-harness "Norwegian ungated" stance). |

GitHub issue **#34** will auto-close when PR #39 merges.

## What's next — concrete acceptance criteria

After merging #39, the highest-leverage follow-ups (ranked by user-visible
impact):

### 1. Per-user account ACL (issue #31 / #8)

Today every `localmail serve` API user has read access to every account.
PR #30 documented this in `add-api-user --help`. To remove the warning:

- New migration `0016_user_accounts.sql`: `user_accounts (user_id, account_id)`.
- Every route that currently receives the global session reads the
  `AuthenticatedUser` from request state and filters by allowed account
  IDs at the SQL boundary.
- New CLI: `localmail grant-account USERNAME ACCOUNT_NAME` / `revoke-account`.
- New tests: "alice can see acct A but not B" cases across messages,
  attachments, changes, search.
- Auto-closes #31 and #8 together.

### 2. Migrate HTML sanitisation from `bleach` to `nh3` (issue #13)

`bleach[css]>=6.2` was an interim hardening in PR #30. `nh3` (ammonia
bindings) is faster, maintained, and the long-term direction. Drop-in for
`bleach.clean` is mostly mechanical but the allowlist surface differs;
golden-file tests in `tests/test_sanitize_*.py` catch regressions.

### 3. Distinguish transient vs poison-pill in `extract_worker` (issue #36)

Mirrors the issue called out in CLAUDE.md for `embed_worker`: today a
network blip during a `docling` model download can mark a perfectly fine
PDF as permanently failed. Scope the "permanent failure" classes narrowly
(parser raises, MIME type wrong, etc.) and let everything else back off
and retry.

### 4. RRF `k` re-tuning (issue #35)

Now that arm 4 (attachment chunks) is in the RRF fusion, the default
`rrf_k = 60` may over-weight large pools. Acceptance harness already
measures recall + MRR — sweep `k ∈ {30, 45, 60, 90}` and pick the best.

### Other open issues worth keeping on the radar

- **#32** Attachment streaming — Range support, Content-Disposition, MIME hardening.
- **#33** Unify ID typing across endpoints (strings on the wire, ints in DB).
- **#37** Unify ConnectionPool ceiling across IDLE/poll/embed/extract workers (+ closes #9).
- **#38** `/v1/changes` semantics — initial backfill window decision.
- **#27 / #28 / #22 / #24 / #18 / #17** GUI client polish & CI.
- **#25** uvicorn / websockets.legacy deprecation (loud warning during tests).
- **#10 / #12** Persist Content-ID on attachments (inline `cid:` rendering).
- **#7** IP-based / global login rate limiter.

## Open decisions & risks

1. **PR #39 unmerged.** SHA `1f34e05` carries the work but isn't on `main`
   yet. Merge via squash to keep the project's PR-driven history.

2. **Norwegian "no" → "nb"/"nn" mismatch.** Lingua returns the variant
   codes, not the umbrella `no`. If a user runs `lang:no`, they get zero
   rows because the column stores `nb` / `nn`. Options: (a) post-process
   `nb`/`nn` → `no` in `LinguaDetector.detect`, (b) document that users
   should query `lang:nb` / `lang:nn`, (c) widen the `lang:` token parser
   to expand `no` to `[no, nb, nn]`. (b) is the lowest-effort and matches
   the existing acceptance harness's Norwegian-ungated stance. Tracked for
   a follow-up if anyone complains.

3. **Lingua confidence threshold.** Default `body_lang_min_confidence=0.65`
   produces 4/50 NULLs on the synthetic corpus — those are 1–2 line
   bodies. Set lower if recall matters more than precision; raise if you
   prefer "no answer" to wrong answers. The `[search]` config table in
   the README documents the knob.

4. **Stale remote branches from previous session.** Still un-pushed-deleted:
   `origin/ci-secret-service-fix`, `origin/gui-client-1/4/5`,
   `origin/gui-server`, `origin/worktree-phase1-hybrid-search`,
   `origin/worktree-phase2-hybrid-search`. Plus the new
   `origin/feat/body-lang-detection` (will auto-delete on merge if you
   tick "Delete branch" in the PR UI).

5. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) still
   open. Run a triage pass before tackling new features.

6. **No ROADMAP.md.** Per-session NEXT_SESSION + open issues currently
   serve as the roadmap. If the project grows beyond ~50 open issues a
   real ROADMAP.md may be worth introducing; today it would just duplicate
   GitHub Issues + this file.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail

# Smoke the feature branch one more time before merging.
git checkout feat/body-lang-detection
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q         # expect 426 passed
unset VIRTUAL_ENV && uv run localmail lang-backfill   # idempotent, no-op if already populated
unset VIRTUAL_ENV && uv run localmail search-status   # check body_lang_populated/pending

# Merge PR #39.
gh pr merge 39 --squash --delete-branch
git checkout main && git pull --ff-only

# Pick the next target. Recommended: issue #31 (per-user account ACL).
gh issue view 31
git checkout -b feat/per-user-account-acl
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
- **Lingua's `with_low_accuracy_mode()`** keeps resident memory at
  ~100 MB instead of ~1 GB; flip `body_lang_low_accuracy = false` if a
  recall regression appears on very short bodies.

## File map (post-PR-#39, on `feat/body-lang-detection`)

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
    extract_worker.py lang_detect.py  ← NEW
    page_cache.py query.py reranker.py searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0015 (no new migration in #39)
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of session. PR #39 is the deliverable; merge it, then move to #31.
