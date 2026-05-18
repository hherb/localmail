# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-18 (late session).** Per-user account ACL is
> **shipped** on `main` (`7f11ac1`), closing issues #31 and #8. Test
> suite green (470 passed, 0 failed) — up from 427 (+43 new tests).
> No PR yet — feature landed directly on `main` per the session's
> "ship fast" mode. Tomorrow's job: open the PR (or merge a new branch),
> push, and then pick the next highest-leverage issue.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read DB
+ attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `a7653b2` | `docs(spec): per-user account ACL design (#31, #8)` — 420-line spec at [docs/superpowers/specs/2026-05-18-per-user-account-acl-design.md](docs/superpowers/specs/2026-05-18-per-user-account-acl-design.md). |
| `7f11ac1` | `feat(serve): per-user account ACL (closes #31, #8)` — 30 files, +1507 / −136. |

Concrete deliverables in `7f11ac1`:

- Migration `0016_user_accounts.sql` — `(user_id, account_id, granted_at)`
  with FK cascades on both sides.
- `src/localmail/api/acl.py` — `allowed_account_ids`, `grant_account`,
  `revoke_account`, `user_has_account`, `grants_for_user`,
  `resolve_user_id_by_username`, `resolve_account_id_by_name`.
- Every service-layer accessor (`api.accounts`, `api.messages`,
  `api.attachments`, `api.search.run_search`) now takes a required
  keyword-only `allowed_account_ids: list[int]`. Empty list → 404 / empty
  list (no leak of resource existence).
- `Searcher.search()` / `continue_page()` / `grow_pool()` gained a
  `user_id` parameter; `PageCache` entries carry the minting user_id and
  reject mismatched callers as cache misses — search cursors can no
  longer be replayed across users.
- `/v1/accounts` `capabilities.is_shared` reflects whether the caller has
  access to ≥ 2 accounts.
- CLI: `localmail grant-account USERNAME ACCOUNT_NAME`,
  `revoke-account USERNAME ACCOUNT_NAME`,
  `list-api-users --with-grants`. The `add-api-user`
  "no per-account ACL" warning is removed; replaced by a one-line hint
  to run `grant-account` next.
- README and CLAUDE.md updated; README has an upgrade section calling
  out that pre-existing single-operator users must run one
  `grant-account` per account they want to keep reading.

Test surface added (+43 tests, 470 total):
- `tests/test_api_acl.py` — 12 tests for the ACL service module
  (insert/idempotent/revoke/cascade/FK violations).
- `tests/test_api_acl_filtering.py` — 12 tests for the accessor filters
  (list_accounts, list_folders, get_message, get_message_raw,
  attachment accessors, is_shared semantics).
- `tests/test_serve_acl_routes.py` — 8 end-to-end tests via TestClient
  with alice + bob holding disjoint grants.
- `tests/test_searcher_acl_cursor.py` — 2 tests for the per-user cache
  scoping invariant on continue_page / grow_pool.
- `tests/test_cli_grant_account.py` — 9 CLI smoke tests against the
  real test DB.

Existing route tests were updated to grant alice access in setup via
a new `tests/conftest.py` fixture `grant_alice_all_accounts`.

## What's next — concrete acceptance criteria

Pick whichever has the most leverage. Ranked:

### 1. Open the ACL PR + push remote

Both commits (`a7653b2` spec, `7f11ac1` feature) are on local `main`.
Before pushing or opening a PR:

- Decide branch model. Two options:
  a. Force-push current `main` to `origin/main` (this is your repo;
     no foreign PRs in flight). Risk: rewriting history if anyone else
     pulled. Safe if you're the sole committer.
  b. Move the two new commits to a `feat/per-user-account-acl` branch,
     reset `main` to `origin/main`, then open PR.
- Push.
- Open PR titled e.g. "feat(serve): per-user account ACL (#31, #8)";
  body should cite both issues with `Closes #31` and `Closes #8`.

Acceptance: PR open, CI green (mirrors local pytest), then merge.

### 2. Migrate HTML sanitisation from `bleach` to `nh3` (#13)

`bleach[css]>=6.2` was an interim hardening in PR #30. `nh3` (ammonia
bindings) is faster, maintained, and the long-term direction. Drop-in
for `bleach.clean` is mostly mechanical; the allowlist surface differs.
`tests/test_sanitize_*.py` golden files catch regressions.

### 3. Distinguish transient vs poison-pill in `extract_worker` (#36)

Mirrors the policy `embed_worker` already enforces. Today a network blip
during a `docling` model download can mark a perfectly fine PDF as
permanently failed. Scope "permanent failure" classes narrowly (parser
raises, MIME mismatch, etc.) and let everything else back off and retry.

### 4. RRF `k` re-tuning (#35)

Now that arm 4 (attachment chunks) is in the RRF fusion, the default
`rrf_k = 60` may over-weight large pools. Acceptance harness already
measures recall + MRR — sweep `k ∈ {30, 45, 60, 90}` and pick the best.

### Other open issues

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

1. **PR vs direct main.** Two commits sit on local `main` only.
   Decide push strategy before next session. No remote work depends on
   these commits.

2. **Existing single-operator upgrade.** Anyone running the previous
   build with a created API user will see empty `/v1/accounts` until
   they run `localmail grant-account USERNAME <each-account>`. README
   has the upgrade note; if there are deployments out there other than
   yours, telegraph this loudly before the next release.

3. **No "grant all" bulk command.** Adding a user with 20 accounts
   means 20 invocations. Acceptable for v1; a `--all` flag is a
   one-liner if it gets painful.

4. **ACL resolver runs per request.** A single `SELECT account_id
   FROM user_accounts WHERE user_id = %s` happens on every authenticated
   API call. Sub-ms in practice but visible if you ever profile a
   high-QPS deployment. Caching is a future option (key by user_id with
   sub-minute TTL).

5. **`websockets.legacy` DeprecationWarning** (issue #25) still fires
   during `test_e2e_serve.py`. Pre-existing — this session didn't
   change it. Tracked.

6. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) surfaced
   during PR #30 still need triage: `gh api
   repos/hherb/localmail/vulnerability-alerts` or the GitHub Security
   tab.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # main, 2 commits ahead of origin
git log --oneline -3                        # verify 7f11ac1, a7653b2 present

# Smoke the merged tree.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 470 passed
unset VIRTUAL_ENV && uv run localmail init-db   # idempotent (applies 0016)
unset VIRTUAL_ENV && uv run localmail list-api-users --with-grants

# Open the PR (option B from "What's next" §1):
git push -u origin main                     # or: git branch feat/per-user-account-acl && reset main
gh pr create --title "feat(serve): per-user account ACL (#31, #8)" \
             --body "Closes #31, #8. See body of 7f11ac1."

# Then triage the next issue:
gh issue list --state open --limit 40

# If you pick #13 (bleach → nh3):
git checkout -b feat/sanitize-via-nh3
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  will pick the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0016 are additive.** Re-running `init-db` on an
  older archive is safe (idempotent), but back up first if the archive
  is non-trivial.
- **`docling` is the Phase 2 extractor.** Install with
  `uv sync --extra extraction` if you need attachment-text extraction
  locally.
- **TLS for `localmail serve`** — `--bind 0.0.0.0` requires `--tls-cert`
  + `--tls-key`. `--no-tls` is only honoured on `127.0.0.1`. Use
  `localmail rotate-tls` to generate a self-signed cert.
- **First-time `body_lang` install** — run `localmail lang-backfill`
  (or `embed-backfill`, which drains both queues) once after upgrade so
  `lang:` queries return rows.
- **ACL upgrade**: post-0016, new API users have **no grants**. Run
  `localmail grant-account USERNAME ACCOUNT_NAME` once per pair.

## File map (post-ACL, current state on `main`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py attachments.py auth.py errors.py messages.py
    sanitize.py search.py
    acl.py                           # NEW — per-user account ACL service
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/                          # auth, accounts, messages, attachments,
                                     # changes, search, version  (all ACL-aware)
  search/
    arms.py chunking.py embed_worker.py embeddings.py extractor.py
    extract_worker.py lang_detect.py page_cache.py query.py reranker.py
    searcher.py                      # learns user_id for cache scoping
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0016_user_accounts.sql
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of per-user-account-ACL session. Two commits on `main`, ready to
push; pick the next issue and ship.
