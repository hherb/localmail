# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-19 (end of session).** PR **#51** (unified
> daemon pool) merged at start of session as squash commit `db6cf44`.
> PR **#52** (user manual) was also merged. This session shipped PR
> **#53** (`feat(serve): force-download + MIME clamp on attachment
> endpoint, closes #32`) — branch
> `feat/attachment-content-disposition`, single commit `0192fcd`,
> **535 tests pass** (524 baseline + 11 new), mypy clean on touched
> files. PR #53 **OPEN** at session close.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `0192fcd` | `feat(serve): force-download + MIME clamp on /v1/attachments/{sha} (closes #32)` — Content-Disposition: attachment with RFC 6266/5987 filename forms, `_INLINE_RISKY_MIMES` clamp set, `Accept-Ranges: none`, new `get_attachment_filename()` helper. |

PR **#53** opened against `main` (status: **OPEN** at session close).

### Issue closed (by PR #53)

- **#32 phase 1** — `api: attachment streaming — Range support,
  Content-Disposition, MIME hardening`. The XSS-relevant pieces ship
  here (Content-Disposition forces browser download; MIME clamp turns
  stored `text/html`/`image/svg+xml` blobs into `application/octet-
  stream` on the wire). The Range piece (`Accept-Ranges: bytes` +
  byte-slice handling) is **deferred to phase 2** — phase 1 sets
  `Accept-Ranges: none` so clients fail fast instead of hanging on
  retry. If you keep #32 open for phase 2, rename it / leave a comment
  noting phase 1 is shipped.

### Concrete deliverables in PR #53

- [`src/localmail/api/attachments.py`](src/localmail/api/attachments.py)
  — adds `get_attachment_filename(conn, sha256_hex, *,
  allowed_account_ids) -> str | None`. Uses the same JSONB-containment
  + GIN-index path as `_caller_can_read_blob`. Deterministic pick
  (`ORDER BY messages.id ASC LIMIT 1`) when multiple carriers use
  different names. Returns None if no carrying message or no
  `filename` key in the JSONB entry.
- [`src/localmail/serve/routes/attachments.py`](src/localmail/serve/routes/attachments.py)
  — rewritten to add named constants `_INLINE_RISKY_MIMES`,
  `_SAFE_FALLBACK_MIME`, `_SHA_PREFIX_LEN_FOR_FALLBACK_NAME`,
  `_QUOTED_STRING_UNSAFE`, `_PRINTABLE_ASCII_MIN`,
  `_PRINTABLE_ASCII_MAX`. Three pure helpers: `_ascii_fallback_name`,
  `_content_disposition`, `_safe_response_mime`, `_fallback_filename`.
  All easily unit-tested via the route tests.
- [`tests/test_api_attachments.py`](tests/test_api_attachments.py)
  — +4 tests: jsonb-filename return, no-acl returns None, no-
  filename-key returns None, deterministic earliest-carrier pick.
  `test_malformed_sha256_raises_validation` now covers
  `get_attachment_filename` too.
- [`tests/test_serve_attachments_routes.py`](tests/test_serve_attachments_routes.py)
  — +7 tests: Content-Disposition: attachment present, unicode
  filename uses RFC 5987 form, fallback when no filename in JSONB,
  quote sanitisation in ASCII fallback, html mime clamped, svg mime
  clamped, safe-mime (PNG) preserved, Accept-Ranges: none header.
  `_seed_blob_with_carrier` gained optional `filename=` /
  `mime=` kwargs.
- [`CLAUDE.md`](CLAUDE.md) — "GUI server (Phase 1 of GUI)" section
  documents the attachment-download policy as a load-bearing
  invariant.

Test surface: 524 → 535 (+11 new tests). No baseline mypy/test
regressions introduced.

## What's next — concrete acceptance criteria

PR #53 needs to merge first. Once it does:

### 1. Merge PR #53 and clean up

```bash
gh pr view 53                       # confirm CI green
gh pr merge 53 --squash --delete-branch
git checkout main && git pull
```

### 2. Pick the next open issue

Top candidates (handoff-prioritised; #37 / #9 / #32-phase1 now closed):

- **#33** Unify ID typing across endpoints (strings on the wire, ints
  in DB). Breaking API change — **best to land now** before more
  external consumers wire up. The in-tree Tauri client is the only
  consumer today.
- **#32 phase 2** Range request support — `Accept-Ranges: bytes` +
  byte-slice handling. Larger scope; matters for big PDFs / video
  attachments and connection-drop resume. Useful but no security
  pressure.
- **#5** Batch INSERT for chunking loop. Perf follow-up; useful once
  archives are large. Touches `embed_worker.py`. Defer until someone
  measures backfill on a 100k+ archive.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on having real operational data showing which classes
  fire. Resist pre-emptive widening.
- **#25** `websockets.legacy` deprecation. **Not actionable** — needs
  upstream uvicorn release with the new `websockets.asyncio` API.
  Re-check on next uvicorn bump.

### Other open issues (unchanged)

- **#38** `/v1/changes` semantics — initial backfill window decision.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`
  rendering).
- **#7** IP-based / global login rate limiter.
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.

## Open decisions & risks

1. **#32 is "closes" in the PR title but only phase 1 is implemented.**
   If you want phase 2 (Range support) tracked separately, re-open #32
   after merge and rename to phase 2, or close #32 and file a follow-up
   issue for Range. Recommend the latter — keeps the issue tracker
   honest about what shipped when.

2. **MIME clamp list is small on purpose.** Only the actively
   script-executable MIMEs (`text/html`, `application/xhtml+xml`,
   `image/svg+xml`, `text/xml`, `application/xml`) are clamped to
   octet-stream. PDFs, images, audio, video, and `text/plain` are
   served with their stored MIME unchanged — Content-Disposition:
   attachment is the actual XSS fix; the clamp is defense-in-depth.
   If a new XSS sink format appears (e.g. some future browser-
   executable container), add it to `_INLINE_RISKY_MIMES` in
   [src/localmail/serve/routes/attachments.py](src/localmail/serve/routes/attachments.py).

3. **Filename pick is deterministic but arbitrary.** When the same
   blob is referenced by multiple ACL-allowed messages with different
   `filename` values, we pick the earliest by `messages.id`. The
   alternative — newest, or "the one in the message the user just
   clicked" — would need a per-request hint from the caller. Not
   worth the complexity until someone complains.

4. **No range support yet.** Big PDFs / videos can't resume on a
   dropped connection and the GUI can't seek into media. `Accept-
   Ranges: none` is honest, but it's a Phase 2 follow-up. Filed under
   #32 phase 2.

5. **`rrf_k=60` is the centre of a flat plateau, not an empirically
   verified optimum** (#35 outcome). Unchanged from prior handoff. No
   action needed until a new corpus measures sensitivity.

6. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
   `main` still need triage. Independent of this PR. Run
   `gh api repos/hherb/localmail/vulnerability-alerts` after merge to
   confirm the count hasn't drifted.

7. **`websockets.legacy` DeprecationWarning** (#25) still fires during
   `test_e2e_serve.py`. Pre-existing; upstream uvicorn blocker. No
   action this session.

8. **Pre-#41 single-operator upgrade.** Anyone running a pre-`4e2e2f1`
   build with a created API user will see empty `/v1/accounts` until
   they run `localmail grant-account USERNAME <each-account>`. README
   upgrade note in place; flag loudly before any release.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # feat/attachment-content-disposition if PR #53 not yet merged

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 535 passed

# Merge PR #53.
gh pr view 53                               # confirm CI green
gh pr merge 53 --squash --delete-branch
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
- **`body_lang` worker index** (#40): the lang-detect claim query
  needs `messages_body_lang_pending_idx` to avoid seq-scan as archives
  grow. The schema test in `tests/test_search_schema.py` enforces both
  the index's existence and its planner eligibility — if the worker
  query shape ever drifts (new predicate columns, different ORDER BY),
  update both the migration's WHERE clause and the EXPLAIN test
  together.
- **Daemon pool sizing** (#37): all daemon threads share `Daemon.pool`.
  Its `max_size` is auto-computed via `db.compute_daemon_pool_size(...)`
  unless `DaemonConfig.pool_max_size` is set. The chosen value is
  logged at startup. When adding a NEW long-running worker thread to
  the daemon, bump the formula (slot per worker) — not just the
  headroom — so the contract stays exact.
- **Attachment download invariants** (#32 phase 1, this PR):
  `/v1/attachments/{sha256}` ALWAYS emits `Content-Disposition:
  attachment` with both `filename=` (ASCII-sanitised) and
  `filename*=UTF-8''...` (percent-encoded original). Stored mime is
  clamped to `application/octet-stream` if it appears in
  `_INLINE_RISKY_MIMES`. `Accept-Ranges: none` is set explicitly.
  Don't remove any of these — the test suite enforces all three. The
  filename helper picks the earliest carrying message by
  `messages.id`; if you change that pick, update
  `test_get_attachment_filename_prefers_first_carrying_message`.

## File map (post-#53, on branch `feat/attachment-content-disposition`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py
    attachments.py                   # +get_attachment_filename() (#32)
    auth.py errors.py messages.py
    sanitize.py
    search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/
      attachments.py                 # Content-Disposition + MIME clamp (#32)
      auth.py accounts.py messages.py changes.py search.py version.py
  daemon.py                          # single shared self.pool for ALL workers
  db.py                              # compute_daemon_pool_size() + constants
  search/
    arms.py chunking.py embed_worker.py embeddings.py
    extractor.py                     # TransientExtractorError (#36)
    extract_worker.py                # uses pool=, not conn_factory= (#37)
    lang_detect.py page_cache.py query.py reranker.py searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0017_messages_body_lang_pending_index.sql
tests/
  acceptance/
    run_recall_eval.py               # Phase 1 multilingual gate
    run_attachment_eval.py           # Phase 2 attachment gate
    run_rrf_k_sweep.py               # #35 sweep harness
  test_api_attachments.py            # +4 filename helper tests (#32)
  test_serve_attachments_routes.py   # +7 Content-Disposition + MIME tests (#32)
  test_daemon_pool.py                # #37 pool-sizing contract
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of attachment-content-disposition session. Single commit `0192fcd`
on `feat/attachment-content-disposition`; PR #53 open. Merge it, then
pick **#33** (ID typing — actionable, prevents future breaking change),
or **#32 phase 2** (Range support), or move on to #5 / #47.
