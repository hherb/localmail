# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-19 (very early session).** Parser-based img-src
> rewriter shipped on branch `fix/sanitize-href-query-corruption`
> (commit `47cf1f3`); PR **#44** open against `main`
> ("fix(api): parser-based img-src rewriter via nh3 attribute_filter
> (closes #43)"). Tests green: **484 passed** (was 482 on main:
> 1 flipped + 2 new = +3 effective additions). mypy clean.
> Per-#42 follow-up — issue #43 ("`?src=…` query string corruption in
> `<a href>`") is closed by this PR. PR #42 itself merged earlier as
> `0837e99` on `main`.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `47cf1f3` | `fix(api): parser-based img-src rewriter via nh3 attribute_filter (closes #13's regex tradeoff, #43)` — branch `fix/sanitize-href-query-corruption`, PR #44. |

Concrete deliverables in `47cf1f3`:

- `src/localmail/api/sanitize.py` — `_rewrite_image_srcs` and
  `_SRC_ATTR_RE` deleted; rewriting moved into a closure produced by
  `_make_attribute_filter(cid_to_sha)` and a pure `_rewrite_img_src`
  helper. nh3's `attribute_filter` callback is parser-aware
  (html5ever), so substrings like `?src=…` inside `<a href>` are never
  confused with `<img src=…>` attributes.
    - `cid` added to `_ALLOWED_URL_SCHEMES` so `cid:` values actually
      reach the filter (nh3 strips disallowed schemes *before* invoking
      the filter — confirmed by repro test).
    - Filter also returns `None` for `cid:` on `<a href>` (defence in
      depth — needed because `cid` is now in `url_schemes` and would
      otherwise survive on link hrefs).
    - Module docstring + the SECURITY-CRITICAL block above
      `_ALLOWED_URL_SCHEMES` rewritten to reflect the new design.
- `tests/test_api_sanitize.py` —
  `test_href_query_string_with_src_param_corrupted` flipped to
  `…_preserved` (now pins the bug fix); 2 new tests:
  `test_href_with_cid_scheme_stripped`,
  `test_anchor_text_containing_img_substring_preserved`.
- Test surface: 26 sanitize tests (was 25); full suite 482 → 484.

Why one PR for one issue: the regex tradeoff documented in
PR #42's follow-up (`3b2e5a3`) was the *known* unresolved item from
that merge. Closing it as its own PR keeps the diff small and the
security review surface tight.

## What's next — concrete acceptance criteria

PR #44 needs to merge first. Once it does:

### 1. Merge PR #44 and clean up

```bash
gh pr view 44                       # confirm CI green
gh pr merge 44 --squash --delete-branch
git checkout main && git pull
```

### 2. Distinguish transient vs poison-pill in `extract_worker` (#36)

Mirrors the policy `embed_worker` already enforces. Today a network blip
during a `docling` model download can mark a perfectly fine PDF as
permanently failed.

Acceptance: scope "permanent failure" classes narrowly (parser raises,
MIME mismatch, byte-stream truncation that survives a retry, etc.) and
let everything else back off and retry without incrementing the failure
counter past `extract_worker_max_retries`. New test covering each
classification. `failed_extractions.error_class` continues to be the
canonical poison-pill marker.

### 3. RRF `k` re-tuning (#35)

Acceptance: with the multilingual acceptance harness
(`tests/acceptance/run_recall_eval.py`), sweep `rrf_k ∈ {30, 45, 60, 90}`
and pick the value that maximises mean recall@20 across de/en/es/ja
without dropping any language below the existing gate (recall@20 ≥ 80%
+ MRR@20 ≥ 0.5). Land the winner as the new default in `SearchConfig`
and add a one-line note in CLAUDE.md.

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

1. **`cid` is now in `_ALLOWED_URL_SCHEMES`.** Necessary for
   `attribute_filter` to see `cid:` values, but it means defence in
   depth in the filter is load-bearing: if anyone widens
   `_ALLOWED_ATTRS` to add another URL-interpreted attribute (e.g.
   `srcset` on `<source>`, `poster` on `<video>`, `formaction` on
   `<button>`), the filter needs a parallel `if attr in {…} and value
   starts with "cid:" → None` clause. Today the filter handles
   `img/src` and `a/href`; that matches the current attribute
   allowlist. Tests cover both.

2. **Tabnabbing protection unchanged.** `<a>` still gets
   `rel="noopener noreferrer"` injected by nh3 — kept from PR #42.

3. **No new dependencies.** Pure refactor inside `sanitize.py`; nh3 has
   supported `attribute_filter` since 0.2.x. `pyproject.toml` unchanged.

4. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
   `main` still need triage. Independent of this PR. Run
   `gh api repos/hherb/localmail/vulnerability-alerts` after merge to
   confirm the count hasn't drifted.

5. **`websockets.legacy` DeprecationWarning** (issue #25) still fires
   during `test_e2e_serve.py`. Pre-existing; this session didn't
   change it. Tracked.

6. **Existing pre-#41 single-operator upgrade.** Anyone running a
   pre-`4e2e2f1` build with a created API user will see empty
   `/v1/accounts` until they run
   `localmail grant-account USERNAME <each-account>`. README upgrade
   note is in place; flag loudly before any release.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # fix/sanitize-href-query-corruption if not yet merged

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 484 passed

# Merge PR #44.
gh pr view 44                               # confirm CI green
gh pr merge 44 --squash --delete-branch
git checkout main && git pull

# Triage the next issue.
gh issue list --state open --limit 40

# If you pick #36 (extract_worker transient vs poison):
git checkout -b fix/extract-worker-transient-vs-poison

# If you pick #35 (RRF k re-tuning):
git checkout -b chore/rrf-k-retune
# Then run the acceptance harness:
unset VIRTUAL_ENV && uv run python tests/acceptance/run_recall_eval.py
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
- **nh3 `attribute_filter` ordering** (new): the callback runs *after*
  the tag/attribute allowlist check but *before* the URL-scheme check.
  Schemes that aren't in `url_schemes` are stripped before the filter
  sees them, so anything the filter must reach must be in
  `_ALLOWED_URL_SCHEMES`. Confirmed via repro; documented above
  `_ALLOWED_URL_SCHEMES`.

## File map (post-#43, on branch `fix/sanitize-href-query-corruption`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py attachments.py auth.py errors.py messages.py
    sanitize.py                      # nh3 attribute_filter-based rewriter
    search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/                          # auth, accounts, messages, attachments,
                                     # changes, search, version  (all ACL-aware)
  search/
    arms.py chunking.py embed_worker.py embeddings.py extractor.py
    extract_worker.py lang_detect.py page_cache.py query.py reranker.py
    searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0016_user_accounts.sql
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of href-query-corruption session. One commit (`47cf1f3`) on
`fix/sanitize-href-query-corruption`; PR #44 open. Merge it, then pick
the next issue (#36 or #35 from the priority list).
