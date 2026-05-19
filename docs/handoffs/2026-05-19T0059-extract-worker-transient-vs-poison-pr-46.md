# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-19 (end of session).** PR **#44** (parser-based
> img-src rewriter, closes #43) already merged earlier today as squash
> commit `d9c7250` on `main` — including the `c122784` follow-up
> (uppercase-cid test + filter/allowlist coupling comment) that landed in
> the same squash. This session shipped **PR #46** for issue **#36**
> (extract_worker transient vs poison-pill classification) — branch
> `fix/extract-worker-transient-vs-poison`, two commits (`5ced39c` +
> `8b802eb`), **497 tests pass** (was 486 on main: +11 new). mypy clean
> for changed files; 4 pre-existing parser.py errors unchanged.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `5ced39c` | `fix(search): distinguish transient vs poison-pill in extract_worker (closes #36)` — TransientExtractorError + `_is_transient` cause-chain walk; 11 new tests (6 unit + 5 integration). |
| `8b802eb` | `docs(readme): document extract_worker transient vs poison-pill policy` — small section under "Recovering from failed messages". |

PR **#46** opened against `main` (status: **OPEN** at session close). Once
merged, branch can be deleted.

Concrete deliverables in PR #46:

- `src/localmail/search/extractor.py` — new `TransientExtractorError(ExtractorError)`
  subclass. Extractors may raise this explicitly when they detect the
  cause is not the blob's fault (model-fetch blip, OCR-OOM, IO timeout).
- `src/localmail/search/extract_worker.py`:
    - `_TRANSIENT_EXC_TYPES = (ConnectionError, TimeoutError, MemoryError)`
      module-level constant. Deliberately narrow — broader (e.g. plain
      `OSError`) would mis-classify permanent ENOENT/EACCES failures as
      transient and let genuinely broken blobs loop forever.
    - `_is_transient(exc)` walks `__cause__` / `__context__` so
      `raise ExtractorError(...) from ConnectionError(...)` (docling's
      wrap pattern) is classified correctly with no extractor code changes.
    - `_process_blob` re-raises transient errors at all three failure
      recording sites (docling raised, lightweight raised + docling empty,
      lightweight raised + no docling). The outer SAVEPOINT handler in
      `run_extract_worker_once`: on transient, ROLLBACK + WARNING log
      + no `failed_extractions` row + no `touched` increment. On
      poison-pill, existing record-as-"unexpected" behavior.
    - Module docstring extended with a new "Transient vs poison-pill"
      section spelling out the policy.
- `tests/test_extract_worker.py` — 11 new tests:
    - 6 unit tests for `_is_transient` (direct TransientExtractorError,
      ConnectionError in cause chain, TimeoutError in cause chain,
      raw MemoryError, rejection of ValueError, rejection of plain
      ExtractorError).
    - 5 integration tests (TransientExtractorError → no failure row;
      ExtractorError caused by ConnectionError → no failure row;
      regression: plain ExtractorError still recorded; batch isolation
      under transient; flaky extractor recovers on second sweep).
- `README.md` — new paragraph under "Recovering from failed messages"
  documenting the transient/poison split for attachment extraction.

Test surface: 486 → 497 (+11). 4 pre-existing `parser.py` mypy errors
unchanged — every other file is mypy-clean.

## What's next — concrete acceptance criteria

PR #46 needs to merge first. Once it does:

### 1. Merge PR #46 and clean up

```bash
gh pr view 46                       # confirm CI green
gh pr merge 46 --squash --delete-branch
git checkout main && git pull
```

### 2. RRF `k` re-tuning (#35)

The acceptance harness has been stable since Phase 2 (arm 4 attachment
chunks added). Default `rrf_k` was set during Phase 1 with only arms 1–3
in the fusion — worth a sweep now that arm 4 is in.

Acceptance: with the multilingual acceptance harness
(`tests/acceptance/run_recall_eval.py`), sweep `rrf_k ∈ {30, 45, 60, 90}`
and pick the value that maximises mean recall@20 across de/en/es/ja
without dropping any language below the existing gate (recall@20 ≥ 80%
+ MRR@20 ≥ 0.5). Land the winner as the new default in `SearchConfig`
and add a one-line note in CLAUDE.md.

```bash
git checkout -b chore/rrf-k-retune
unset VIRTUAL_ENV && uv run python tests/acceptance/run_recall_eval.py
```

### 3. Investigate `<a href="data:...">` (#45)

Tracked as a separate XSS surface. Pre-existing pre-#44 — c122784 noted
it but explicitly scoped out. Likely follows the same `attribute_filter`
pattern: on `a/href` whose value starts with `data:`, return None.

### Other open issues (unchanged from previous handoff)

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
- **#25** uvicorn / `websockets.legacy` deprecation (still firing
  during `test_e2e_serve.py`).
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`
  rendering).
- **#7** IP-based / global login rate limiter.
- **#5** Batch INSERT for chunking loop.
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#3** `db._split_statements` is `sqlparse`-backed; verify before
  closing.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.

## Open decisions & risks

1. **Transient allowlist intentionally narrow.** Only
   `TransientExtractorError`, `ConnectionError`, `TimeoutError`,
   `MemoryError`. If docling raises an HTTP/IO error class outside this
   set (e.g. `requests.exceptions.ConnectionError` is NOT a builtin
   `ConnectionError`), it'll be classified as permanent and bump
   `retry_count`. Mitigation paths: (a) docling code in
   `extractor.py::DoclingExtractor.extract` could be tightened to
   detect the third-party class and raise `TransientExtractorError`
   explicitly; (b) add the class to `_TRANSIENT_EXC_TYPES` once it's
   seen in real operations. Today's narrow set protects against the
   reverse failure mode (mis-classifying ENOENT/EACCES as transient
   and looping forever).

2. **No `failed_extractions` row for transient failures.** This means
   `localmail list-failed-extractions` won't surface them — they're
   only visible via WARNING logs. Acceptable trade-off for now; if
   transients turn out to need their own visibility surface, add a
   `transient_extractions` view backed by log scraping or a counter
   table.

3. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
   `main` still need triage. Independent of this PR. Run
   `gh api repos/hherb/localmail/vulnerability-alerts` after merge to
   confirm the count hasn't drifted.

4. **`websockets.legacy` DeprecationWarning** (issue #25) still fires
   during `test_e2e_serve.py`. Pre-existing; this session didn't
   change it. Tracked.

5. **Pre-#41 single-operator upgrade.** Anyone running a
   pre-`4e2e2f1` build with a created API user will see empty
   `/v1/accounts` until they run
   `localmail grant-account USERNAME <each-account>`. README upgrade
   note is in place; flag loudly before any release.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # fix/extract-worker-transient-vs-poison if PR #46 not yet merged

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 497 passed

# Merge PR #46.
gh pr view 46                               # confirm CI green
gh pr merge 46 --squash --delete-branch
git checkout main && git pull

# Triage the next issue.
gh issue list --state open --limit 40

# If you pick #35 (RRF k re-tuning):
git checkout -b chore/rrf-k-retune
unset VIRTUAL_ENV && uv run python tests/acceptance/run_recall_eval.py

# If you pick #45 (a href="data:" XSS):
git checkout -b fix/sanitize-href-data-scheme
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  picks the wrong interpreter without this.
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
- **nh3 style-attribute whitespace**: nh3 emits compact
  `color:red` (no space after `:`); never assert exact whitespace in
  sanitiser tests — assert that the property and value both survived.
- **nh3 `attribute_filter` ordering**: the callback runs *after* the
  tag/attribute allowlist check but *before* the URL-scheme check.
  Schemes that aren't in `url_schemes` are stripped before the filter
  sees them, so anything the filter must reach must be in
  `_ALLOWED_URL_SCHEMES`.
- **extract_worker transient classification** (new): an exception is
  transient iff `isinstance(e, TransientExtractorError)` OR an instance
  of `(ConnectionError, TimeoutError, MemoryError)` appears anywhere in
  its `__cause__` / `__context__` chain. Third-party HTTP/IO classes
  (e.g. `requests.exceptions.ConnectionError`) are NOT in the set —
  extractors must raise `TransientExtractorError` explicitly to opt them
  in. Add to `_TRANSIENT_EXC_TYPES` only after seeing the class in real
  operations (avoid pre-emptive widening).

## File map (post-#46, on branch `fix/extract-worker-transient-vs-poison`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py attachments.py auth.py errors.py messages.py
    sanitize.py search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/                          # auth, accounts, messages, attachments,
                                     # changes, search, version  (all ACL-aware)
  search/
    arms.py chunking.py embed_worker.py embeddings.py
    extractor.py                     # +TransientExtractorError (#36)
    extract_worker.py                # +_is_transient + transient backoff (#36)
    lang_detect.py page_cache.py query.py reranker.py searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0016_user_accounts.sql
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of extract-worker-transient session. Two commits (`5ced39c`,
`8b802eb`) on `fix/extract-worker-transient-vs-poison`; PR #46 open.
Merge it, then pick #35 (RRF k retune) or #45 (data: XSS) next.
