# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-19 (end of session).** PR **#46** (extract_worker
> transient vs poison-pill, closes #36) merged at the start of this session
> as squash commit `5d93556` on `main`. This session shipped **PR #48** for
> issue **#45** (drop `data:` scheme on `<a href>`) — branch
> `fix/sanitize-href-data-scheme`, one commit (`76d787c`), **501 tests pass**
> (was 497 on main: +3 new sanitize tests + 1 pre-existing baseline drift).
> mypy clean on the changed file.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `76d787c` | `fix(sanitize): drop data: scheme on <a href> (closes #45)` — extend `<a href>` deny list in `_make_attribute_filter` from `cid:` to `(cid:, data:)` via new module-level `_HREF_DENY_SCHEMES` tuple; 3 new sanitize tests. |

PR **#48** opened against `main` (status: **OPEN** at session close). Once
merged, branch can be deleted.

Concrete deliverables in PR #48:

- `src/localmail/api/sanitize.py`:
    - New module-level `_HREF_DENY_SCHEMES: tuple[str, ...] = ("cid:", "data:")`
      with a SECURITY-CRITICAL comment explaining why both schemes must be
      allowed in `_ALLOWED_URL_SCHEMES` (so they reach the
      `attribute_filter` for img/src handling) yet dropped from `<a href>`.
    - `_make_attribute_filter` updated: `if attr == "href" and
      value.lower().startswith(_HREF_DENY_SCHEMES): return None`. Drops
      `mailto:`/`http(s)://` continue to work unchanged; only `cid:` and
      `data:` on `<a href>` are dropped. Case-insensitive (lowercase prefix
      compare, RFC 3986).
    - Docstring on `_make_attribute_filter` updated to describe both
      schemes' deny paths; `_ALLOWED_URL_SCHEMES` block-comment updated
      to flag the parallel `data:` href risk.
- `tests/test_api_sanitize.py` — 3 new tests:
    - `test_href_with_data_scheme_stripped` — `<a href="data:text/html,<script>alert(1)</script>">click</a>`
      → href + script dropped, link text survives.
    - `test_href_with_uppercase_data_scheme_stripped` — case-insensitive
      (`DATA:`, `Data:`, `dAtA:` all dropped); pins the `.lower()`.
    - `test_data_substring_in_title_attribute_preserved` — over-reach
      regression test; `title="see data:image/png... discussion"`
      survives intact (the filter is `href`-only).

Test surface: 497 → 501 (+3 new; +1 from a baseline drift between handoffs,
likely a sanitize test gained earlier). 4 pre-existing `parser.py` mypy
errors unchanged — every other file is mypy-clean.

## What's next — concrete acceptance criteria

PR #48 needs to merge first. Once it does:

### 1. Merge PR #48 and clean up

```bash
gh pr view 48                       # confirm CI green
gh pr merge 48 --squash --delete-branch
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

### 3. extract_worker third-party transient classes (#47)

Follow-up to PR #46. Today `_TRANSIENT_EXC_TYPES = (ConnectionError,
TimeoutError, MemoryError)` covers builtins only. Third-party HTTP/IO
classes (e.g. `requests.exceptions.ConnectionError`) are NOT in the set —
extractors must raise `TransientExtractorError` explicitly to opt them in.

Acceptance: identify which third-party transient classes actually fire
in real operations (after PR #46 has run in production for a while),
then either (a) tighten the relevant `extract` method to wrap them in
`TransientExtractorError` or (b) add the class to `_TRANSIENT_EXC_TYPES`.
Defer until we have observation data — avoid pre-emptive widening that
risks mis-classifying permanent ENOENT/EACCES as transient.

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

1. **`data:` deny is defence-in-depth, not the primary control.** Modern
   browsers block or sandbox top-level navigation to `data:` URLs
   (Firefox since 59, Chrome since ~60); scripts in navigated `data:`
   URLs run in opaque origins. The serve middleware's CSP is the
   intended backstop. The sanitiser change closes the "what if a future
   client opens links via `window.open` or shells to a system browser"
   gap. If a render path is ever added that bypasses CSP, this defence
   becomes load-bearing.

2. **The `_HREF_DENY_SCHEMES` allowlist of denies is closed.** Any new
   `_ALLOWED_URL_SCHEMES` member that is rendered by the browser
   (e.g. `file:`, `vbscript:` should that ever be considered) must be
   evaluated for whether it needs to be added to `_HREF_DENY_SCHEMES`
   in parallel. The existing inline comment on `_ALLOWED_URL_SCHEMES`
   flags this requirement.

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

6. **Transient allowlist intentionally narrow** (from #36, still open).
   Only `TransientExtractorError`, `ConnectionError`, `TimeoutError`,
   `MemoryError`. Third-party classes (e.g. `requests.exceptions.
   ConnectionError`) are NOT builtins — tracked as #47. Defer until
   real-world observation data is available.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # fix/sanitize-href-data-scheme if PR #48 not yet merged

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 501 passed

# Merge PR #48.
gh pr view 48                               # confirm CI green
gh pr merge 48 --squash --delete-branch
git checkout main && git pull

# Triage the next issue.
gh issue list --state open --limit 40

# If you pick #35 (RRF k re-tuning):
git checkout -b chore/rrf-k-retune
unset VIRTUAL_ENV && uv run python tests/acceptance/run_recall_eval.py

# If you pick #47 (third-party transient classes):
git checkout -b feat/extractor-transient-third-party
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
- **extract_worker transient classification**: an exception is
  transient iff `isinstance(e, TransientExtractorError)` OR an instance
  of `(ConnectionError, TimeoutError, MemoryError)` appears anywhere in
  its `__cause__` / `__context__` chain. Third-party HTTP/IO classes
  (e.g. `requests.exceptions.ConnectionError`) are NOT in the set —
  extractors must raise `TransientExtractorError` explicitly to opt them
  in. Add to `_TRANSIENT_EXC_TYPES` only after seeing the class in real
  operations (avoid pre-emptive widening).
- **`<a href>` deny schemes** (new, #48): `_HREF_DENY_SCHEMES = ("cid:",
  "data:")`. Both schemes must remain in `_ALLOWED_URL_SCHEMES` so they
  reach the `attribute_filter` for img/src handling (cid → attachment
  URL rewrite; data:image/... validated by `_DATA_IMAGE_RE`). Any new
  URL scheme added to `_ALLOWED_URL_SCHEMES` that a browser would
  dereference on `<a href>` MUST be considered for parallel addition
  to `_HREF_DENY_SCHEMES`.

## File map (post-#48, on branch `fix/sanitize-href-data-scheme`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py attachments.py auth.py errors.py messages.py
    sanitize.py                      # +_HREF_DENY_SCHEMES (#45)
    search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/                          # auth, accounts, messages, attachments,
                                     # changes, search, version  (all ACL-aware)
  search/
    arms.py chunking.py embed_worker.py embeddings.py
    extractor.py                     # TransientExtractorError (#36)
    extract_worker.py                # _is_transient + transient backoff (#36)
    lang_detect.py page_cache.py query.py reranker.py searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0016_user_accounts.sql
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of sanitize-href-data session. One commit (`76d787c`) on
`fix/sanitize-href-data-scheme`; PR #48 open. Merge it, then pick #35
(RRF k retune) or #47 (third-party transient classes) next.
