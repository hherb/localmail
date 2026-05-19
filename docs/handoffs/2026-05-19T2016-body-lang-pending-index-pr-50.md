# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-19 (end of session).** PR **#49** (RRF k sweep
> harness for #35) was merged to `main` at session start as squash
> commit `48e371a`. This session shipped **PR #50** (partial index for
> the body_lang lang-detect claim query, closes #40) — branch
> `chore/messages-body-lang-pending-index`, two commits (`81ed6f9`,
> `0e8fea0`), **516 tests pass** (514 baseline + 2 new schema tests),
> mypy clean on new code. PR #50 currently **OPEN** at session close.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `81ed6f9` | `feat(search): partial index for body_lang lang-detect claim query (closes #40)` — migration `0017_messages_body_lang_pending_index.sql` + two schema tests. |
| `0e8fea0` | `docs: bump CLAUDE.md migration pointers to 0017` — file map and "next migration" pointer updated. |

PR **#50** opened against `main` (status: **OPEN** at session close).

### Issue closed

- **#40** — Partial index on `messages(id) WHERE body_lang IS NULL AND
  body_text IS NOT NULL` for the lang-detect worker. The existing
  `messages_body_lang_idx` (added by 0015) only covered the inverse
  predicate (`body_lang IS NOT NULL`), so `run_lang_detect_pass`'s claim
  query fell back to a seq scan that gets slower as labelled fraction
  approaches 100%. Migration 0017 adds the symmetric partial index,
  predicate matches the worker query verbatim. Two schema tests:
  canonical predicate text in `pg_indexes.indexdef`, and an `EXPLAIN`
  under `SET LOCAL enable_seqscan = off` that confirms the planner is
  willing to use the new index for the worker query.

### Concrete deliverables in PR #50

- [`migrations/0017_messages_body_lang_pending_index.sql`](migrations/0017_messages_body_lang_pending_index.sql)
  — `CREATE INDEX IF NOT EXISTS messages_body_lang_pending_idx ON
  messages (id) WHERE body_lang IS NULL AND body_text IS NOT NULL`.
  Idempotent; safe to re-run.
- [`tests/test_search_schema.py`](tests/test_search_schema.py) — added
  `test_messages_body_lang_pending_index_exists` (predicate text check)
  and `test_messages_body_lang_pending_index_is_eligible_for_worker_query`
  (EXPLAIN with `enable_seqscan = off`).
- [`CLAUDE.md`](CLAUDE.md) — file map and migration-version pointer
  bumped to 0017.

Test surface: 514 → 516 (+2 new schema tests). Pre-existing mypy errors
in `src/localmail/parser.py` (4 errors) untouched — unrelated to this
change.

## What's next — concrete acceptance criteria

PR #50 needs to merge first. Once it does:

### 1. Merge PR #50 and clean up

```bash
gh pr view 50                       # confirm CI green
gh pr merge 50 --squash --delete-branch
git checkout main && git pull
```

### 2. Pick the next open issue

Top candidates (handoff-prioritised; #40 now closed):

- **#37** Unify ConnectionPool ceiling across IDLE/poll/embed/extract
  workers (closes #9 too). Acceptance: one config knob
  (`workers_pool_size`?), all four worker paths use it, daemon startup
  log reports the value. Touches `daemon.py`, `idle.py`, `poller.py`,
  `embed_worker.py`, `extract_worker.py`. Add a test for the env-var
  override.
- **#25** `websockets.legacy` deprecation. Likely a one-line `import`
  fix + a re-run of `test_e2e_serve.py` to confirm the warning is gone.
- **#5** Batch INSERT for chunking loop. Perf follow-up; useful once
  archives are large.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on having real operational data showing which classes
  fire. Resist pre-emptive widening.

### Other open issues (unchanged)

- **#38** `/v1/changes` semantics — initial backfill window decision.
- **#32** Attachment streaming — Range support, Content-Disposition,
  MIME hardening.
- **#33** Unify ID typing across endpoints (strings on the wire, ints
  in DB).
- **#27 / #28 / #22 / #24 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`
  rendering).
- **#7** IP-based / global login rate limiter.
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.

## Open decisions & risks

1. **Migration 0017 is plain `CREATE INDEX`, not `CONCURRENTLY`.** Mirrors
   0015's style (the symmetric partial index) and the worker is the only
   consumer of the new index, so write contention during build is
   bounded by the existing lang-backfill cadence. If a production
   archive sees a long-running build that blocks the daemon, fall back
   to `CREATE INDEX CONCURRENTLY` via a `-- @non-transactional` marker
   (see migration 0013 for prior art). Same eventual treatment as #2.

2. **RRF `rrf_k=60` is the centre of a flat plateau, not an empirically
   verified optimum** (#35 outcome). No measurable harm in changing it
   within [10, 180] on the current synthetic corpora, but no measurable
   benefit either. Keep the conventional default until production data
   shows otherwise. Sweep tool retained for the next attempt.

3. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
   `main` still need triage. Independent of this PR. Run
   `gh api repos/hherb/localmail/vulnerability-alerts` after merge to
   confirm the count hasn't drifted.

4. **`websockets.legacy` DeprecationWarning** (#25) still fires during
   `test_e2e_serve.py`. Pre-existing; tracked.

5. **Pre-#41 single-operator upgrade.** Anyone running a pre-`4e2e2f1`
   build with a created API user will see empty `/v1/accounts` until
   they run `localmail grant-account USERNAME <each-account>`. README
   upgrade note is in place; flag loudly before any release.

6. **Transient allowlist intentionally narrow** (#47, still open). Only
   `TransientExtractorError`, `ConnectionError`, `TimeoutError`,
   `MemoryError`. Third-party classes (e.g.
   `requests.exceptions.ConnectionError`) are NOT builtins — tracked
   as #47. Defer until real-world observation data is available.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # chore/messages-body-lang-pending-index if PR #50 not yet merged

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 516 passed

# Merge PR #50.
gh pr view 50                               # confirm CI green
gh pr merge 50 --squash --delete-branch
git checkout main && git pull

# Triage the next issue.
gh issue list --state open --limit 40
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  picks the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0017 are additive.** Re-running `init-db` on an
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
- **nh3 style-attribute whitespace**: nh3 emits compact
  `color:red` (no space after `:`); never assert exact whitespace in
  sanitiser tests — assert that the property and value both survived.
- **nh3 `attribute_filter` ordering**: the callback runs *after* the
  tag/attribute allowlist check but *before* the URL-scheme check.
  Schemes that aren't in `url_schemes` are stripped before the filter
  sees them, so anything the filter must reach must be in
  `_ALLOWED_URL_SCHEMES`.
- **extract_worker transient classification**: an exception is
  transient iff `isinstance(e, TransientExtractorError)` OR an instance
  of `(ConnectionError, TimeoutError, MemoryError)` appears anywhere in
  its `__cause__` / `__context__` chain. Third-party HTTP/IO classes
  (e.g. `requests.exceptions.ConnectionError`) are NOT in the set —
  extractors must raise `TransientExtractorError` explicitly to opt
  them in. Add to `_TRANSIENT_EXC_TYPES` only after seeing the class in
  real operations (avoid pre-emptive widening).
- **`<a href>` deny schemes** (#48): `_HREF_DENY_SCHEMES = ("cid:",
  "data:")`. Both schemes must remain in `_ALLOWED_URL_SCHEMES` so they
  reach the `attribute_filter` for img/src handling (cid → attachment
  URL rewrite; data:image/... validated by `_DATA_IMAGE_RE`). Any new
  URL scheme added to `_ALLOWED_URL_SCHEMES` that a browser would
  dereference on `<a href>` MUST be considered for parallel addition
  to `_HREF_DENY_SCHEMES`. The deny prefix match strips leading C0
  controls + ASCII whitespace first (mirrors WHATWG URL parser; see
  `_LEADING_URL_TRIM_RE`).
- **RRF fusion is robust but un-tuned against production data** (#35
  outcome): synthetic corpora are insensitive to `rrf_k` because one
  arm dominates rank-1. Don't conclude that the constant doesn't
  matter — only that the synthetic harness can't measure it. Use
  `tests/acceptance/run_rrf_k_sweep.py --corpus {multilingual,
  attachment}` to re-measure against any new corpus before tuning.
- **`body_lang` worker index** (#40, this PR): the lang-detect claim
  query needs `messages_body_lang_pending_idx` to avoid seq-scan as
  archives grow. After PR #50 merges, the schema test in
  `tests/test_search_schema.py` enforces both the index's existence and
  its planner eligibility — if the worker query shape ever drifts (new
  predicate columns, different ORDER BY), update both the migration's
  WHERE clause and the EXPLAIN test together.

## File map (post-#50, on branch `chore/messages-body-lang-pending-index`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py attachments.py auth.py errors.py messages.py
    sanitize.py
    search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/                          # auth, accounts, messages, attachments,
                                     # changes, search, version (all ACL-aware)
  search/
    arms.py chunking.py embed_worker.py embeddings.py
    extractor.py                     # TransientExtractorError (#36)
    extract_worker.py                # _is_transient + transient backoff (#36)
    lang_detect.py page_cache.py query.py reranker.py searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0017_messages_body_lang_pending_index.sql
tests/
  acceptance/
    run_recall_eval.py               # Phase 1 multilingual gate
    run_attachment_eval.py           # Phase 2 attachment gate
    run_rrf_k_sweep.py               # #35 sweep harness
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of body-lang-pending-index session. Two commits (`81ed6f9`,
`0e8fea0`) on `chore/messages-body-lang-pending-index`; PR #50 open.
Merge it, then pick #37 (unified pool ceiling), #25 (websockets
deprecation), or another from the open list.
