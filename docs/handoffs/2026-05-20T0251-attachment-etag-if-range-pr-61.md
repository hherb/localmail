# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-20 (end of session).** PR **#60** (short-read
> WARNING on the attachment stream, closes #58) merged at session start
> as squash commit `06e379a`. Three additional commits had also landed
> on `main` between sessions — `0f8f508` (fastembed bge-reranker-v2-m3
> compat fix), `e915550` (GUI formatError centralisation + RFC 7807
> surfacing), and `cb7d1e4` (rerank pool right-sizing + warm session).
>
> This session shipped PR **#61**
> (`feat(serve): ETag + If-None-Match + If-Range on /v1/attachments,
> closes #59`) — branch `feat/attachment-etag-if-range`, single commit
> `fc7c2a0`, **634 tests pass** (597 baseline + 37 new in this PR),
> mypy clean on touched files. PR #61 **OPEN** at session close.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `fc7c2a0` | `feat(serve): ETag + If-None-Match + If-Range on /v1/attachments (closes #59)` — `/v1/attachments/{sha256}` now advertises a strong `ETag: "<sha256-hex>"` on every 200 / 206 / 416 response. `If-None-Match` (weak compare, `*` accepted) short-circuits to 304 Not Modified with an empty body, evaluated **before** Range per RFC 9110 §13.2.2. `If-Range` (strong compare only — weak / HTTP-date / garbage all reject) on a request that also carries `Range` either lets the partial through or — on mismatch — falls back to a full 200 so a resumed download cannot stitch onto a stale prefix. Parsing lives in a new pure module [`src/localmail/api/conditional.py`](src/localmail/api/conditional.py) so future transports (MCP, etc.) reuse it. CLAUDE.md documents the new invariants next to the existing #32 / #54 / #58 attachment-download blocks. 37 new tests: 23 pure-parser units + 14 route-level integration tests. |

PR **#61** opened against `main` (status: **OPEN** at session close).

### Issue closed (by PR #61)

- **#59** — `Add ETag and If-Range support to /v1/attachments/{sha256}`.
  All three scope items covered: ETag emission (always), If-None-Match
  → 304 (with weak compare + `*` + comma-list), If-Range → strong-only
  match (otherwise serve 200 full). 304 carries the ETag header but
  no Content-Disposition / Accept-Ranges, per RFC 9110 §15.4.5
  representation-metadata rules — minimal, spec-clean.

### Concrete deliverables in PR #61

- New [`src/localmail/api/conditional.py`](src/localmail/api/conditional.py)
  (111 lines) — pure module with three helpers:
  - `etag_for_sha256(sha256_hex)` — strong-form, DQUOTE-wrapped, no
    `W/` prefix.
  - `if_none_match_satisfies(header, etag)` — weak compare; handles
    `*`, comma-separated list with OWS, weak-tag normalisation.
  - `if_range_allows_partial(header, etag)` — strong compare; weak
    tag / HTTP-date / garbage / empty all fail closed (caller serves
    200 full); absent header passes through (no precondition).
- [`src/localmail/serve/routes/attachments.py`](src/localmail/serve/routes/attachments.py)
  — `stream_blob` gained:
  - One `etag = etag_for_sha256(sha256)` derivation.
  - Pre-Range `if_none_match_satisfies` check → 304 with `ETag` header.
  - Pre-parse `if_range_allows_partial` check that nullifies
    `range_header` when the precondition fails.
  - `ETag` header added to all four other response paths (200 streaming,
    206 streaming, 416, no-Range 200).
  - New constant `_HTTP_NOT_MODIFIED = 304`.
- New [`tests/test_api_conditional.py`](tests/test_api_conditional.py)
  (132 lines, 23 tests) — pure-function units covering exact / weak /
  `*` / comma-list / OWS / HTTP-date / garbage / empty across both
  comparison rules.
- New [`tests/test_serve_attachments_conditional.py`](tests/test_serve_attachments_conditional.py)
  (387 lines, 14 tests) — route-level integration:
  - ETag emission on 200 / 206 / 416.
  - If-None-Match → 304 with strong / weak / `*` / non-matching /
    Range-precedence variants (5 tests).
  - If-Range with Range: matching → 206, non-matching → 200 full,
    weak-tag → 200, HTTP-date → 200, without Range → no-op (5 tests).
  - If-Range mismatch preserves the #32 force-download invariants
    (Content-Disposition + MIME clamp) — guards against a fix that
    bypasses the security headers when it short-circuits the Range
    path.
- [`CLAUDE.md`](CLAUDE.md) — new "Conditional GET — ETag /
  If-None-Match / If-Range (#59)" bullet next to the existing #32
  (force-download), #54 (Range), #58 (short-read) blocks. Documents:
  - Why the ETag is canonically strong and immutable (SHA-keyed URLs).
  - Why `If-None-Match` uses weak compare and is evaluated **before**
    Range.
  - Why `If-Range` uses strong compare and falls back to 200 full on
    mismatch.
  - That 304 carries `ETag` only (no Content-Disposition /
    Accept-Ranges) per §15.4.5.
  - That `etag_for_sha256` returns the already-quoted form — don't
    double-quote.

Test surface: 597 → 634 (+37 in this PR).

## What's next — concrete acceptance criteria

PR #61 needs to merge first. Once it does:

### 1. Merge PR #61 and clean up

```bash
gh pr view 61                       # confirm CI green
gh pr merge 61 --squash --delete-branch
git checkout main && git pull
```

### 2. Pick the next open issue

Top candidates (handoff-prioritised; #54 / #58 / #59 / #32-phase1 /
#33 / #37 / #9 closed by recent sessions):

- **#38** `/v1/changes` semantics — pick one of: keep tail-subscription
  + point clients at `/v1/messages` for backfill; add `min_id`; add
  a separate paginated `/v1/messages?since=&before=` endpoint. Best
  decided after observing what the GUI actually does in production.
  May still be premature.
- **#5** Batch INSERT for chunking loop. Perf follow-up; useful once
  archives are large. Touches `embed_worker.py`. Defer until someone
  measures backfill on a 100k+ archive.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on having real operational data showing which classes
  fire. Resist pre-emptive widening.
- **#25** `websockets.legacy` deprecation. **Not actionable** — needs
  upstream uvicorn release with the new `websockets.asyncio` API.
  Re-check on next uvicorn bump.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`
  rendering).
- **#7** IP-based / global login rate limiter.
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.

**Recommendation**: do **the test-file split** next — `tests/
test_serve_attachments_routes.py` is now 587 lines (unchanged from
the previous handoff) and was already flagged as a "split candidate."
A natural split: pull the #54 (Range) and #58 (short-read) blocks into
`tests/test_serve_attachments_streaming.py`. The new #59 conditional
tests are already in their own file (`test_serve_attachments_conditional.py`,
387 lines). This is a no-feature housekeeping PR that prevents the
routes-tests file from growing further. Alternative: **#7** (IP
rate limiter on login) is well-scoped and has clear acceptance
criteria.

## Open decisions & risks

1. **304 carries `ETag` only (#59).** Per RFC 9110 §15.4.5, "a 304
   response MUST NOT generate representation metadata other than
   Content-Location, ETag, Vary, Cache-Control, Expires" — we emit
   ETag and nothing else. Notably: no Content-Disposition, no
   Accept-Ranges, no Content-Length on 304. If a downstream proxy
   ever complains about a missing Accept-Ranges on 304, the cache
   that produced the 304 should be re-using its previous 200's
   headers anyway.

2. **If-Range with HTTP-date always fails (#59).** We don't track
   per-blob mtime, so we can't strong-validate an HTTP-date If-Range.
   Conservative fallback to 200. Anyone wanting date-validated
   If-Range must first wire `Last-Modified` semantics into
   `attachment_blobs` and the route, which is a separate (and
   probably-pointless given SHA URLs) project.

3. **If-Range strong-match is via exact-string equality** —
   `if_range_allows_partial` compares `header.strip() == etag` after
   confirming the candidate starts with `"`. Because `etag_for_sha256`
   always emits exactly `"<64 lowercase hex>"`, this is fine; any
   future ETag scheme that allows opaque tags with escaped DQUOTEs
   would need a proper etag-parse helper. Out of scope for #59.

4. **Short-read response shape is unchanged (#58).** Carried forward.
   When the on-disk blob is truncated below the DB `size_bytes`, we
   still emit the advertised `Content-Length` and the client sees a
   connection that ends early. WARNING log is the only ops signal.

5. **`bytes=0-<huge>` returns 206 with end-clamped, not 416** —
   carried forward from #54. Per RFC 9110 §14.1.2 end-past-EOF gets
   clamped to size-1.

6. **Multi-range falls through to 200, not 416 or
   `multipart/byteranges`.** Carried forward. Single-range covers
   PDF/video seek + connection resume; that's all the GUI needs.

7. **`Accept-Ranges: bytes` is always advertised** on 200 / 206 / 416.
   On 304 we deliberately do not include it (§15.4.5 minimalism).

8. **`parse_int_id` from PR #55 — leading zeros are accepted but
   `"01"` resolves to `1`.** Carried forward. Document the canonical
   form in the OpenAPI spec when we add one.

9. **MIME clamp list (from PR #53) is small on purpose.** Only
   actively script-executable MIMEs are clamped to octet-stream.
   Carried forward.

10. **`rrf_k=60` is the centre of a flat plateau, not an empirically
    verified optimum** (#35 outcome). Unchanged.

11. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
    `main` still need triage. Independent of this PR. Run
    `gh api repos/hherb/localmail/vulnerability-alerts` after merge
    to confirm the count hasn't drifted.

12. **`websockets.legacy` DeprecationWarning** (#25) still fires
    during `test_e2e_serve.py`. Pre-existing; upstream uvicorn
    blocker. No action this session.

13. **Pre-#41 single-operator upgrade.** Anyone running a pre-`4e2e2f1`
    build with a created API user will see empty `/v1/accounts`
    until they run `localmail grant-account USERNAME <each-account>`.
    README upgrade note in place; flag loudly before any release.

14. **`tests/test_serve_attachments_routes.py` is now 587 lines.**
    Unchanged from previous handoff — the new #59 tests went into
    `test_serve_attachments_conditional.py` (387 lines), so the
    routes-tests file did NOT grow further. Still a soft split
    candidate. Reasonable place to split would be: pull the `#54`
    Range block and the `#58` short-read block into a dedicated
    `tests/test_serve_attachments_streaming.py`. Not urgent.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # main; possibly with the prior session's
                                           # NEXT_SESSION.md untouched if you've already
                                           # checked out feat/attachment-etag-if-range

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 634 passed

# Merge PR #61.
gh pr view 61                               # confirm CI green
gh pr merge 61 --squash --delete-branch
git checkout main && git pull

# Next: test-file split (housekeeping) OR an open issue.
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
- **Attachment download invariants** (#32 phase 1 + #54 phase 2 +
  #58 phase 3 + #59 phase 4): `/v1/attachments/{sha256}` ALWAYS emits
  `Content-Disposition: attachment` with both `filename=`
  (ASCII-sanitised) and `filename*=UTF-8''...` (percent-encoded
  original). Stored mime is clamped to `application/octet-stream` if
  it appears in `_INLINE_RISKY_MIMES`. `Accept-Ranges: bytes` and
  `ETag: "<sha>"` are set on every 200 / 206 / 416 response. A
  short-read against the on-disk blob (DB size > file size) emits a
  `WARNING` on the `localmail.serve` logger but the response shape
  is unchanged. A conditional GET (`If-None-Match` matches) returns
  304 with `ETag` only — no Content-Disposition, no Accept-Ranges
  (§15.4.5).
- **Range parsing boundary** (#54): `localmail.api.range_requests.
  parse_byte_range` is the only path that interprets `Range: bytes=…`.
  `None` return → caller serves 200; `UnsatisfiableRange` exception →
  caller serves 416 with `unsatisfiable_content_range(size)`. Don't
  parse Range inline in route handlers — extend the pure module so
  new transports (MCP, etc.) get the same behaviour for free.
- **Conditional parsing boundary** (#59): `localmail.api.conditional`
  is the only path that interprets `If-None-Match` and `If-Range`.
  `etag_for_sha256` is the only path that builds an ETag header
  value. Don't inline-compare these in route handlers — extend the
  pure module so future transports (MCP, etc.) get the same
  behaviour for free.
- **Streaming WARNING contract** (#58): any new streaming endpoint
  that advertises a `Content-Length` MUST also count bytes yielded
  and call `_log_truncation()` (or an analogue) when the source runs
  short. Currently only `_stream_full` and `_stream_range` in the
  attachment route do this; if a future endpoint adds streaming, it
  inherits the contract or the route docstring is lying.
- **ID-typing boundary** (#33): path and query parameters bearing
  entity IDs are typed `str` on the route handler signature, and
  `localmail.api.ids.parse_int_id(value, field="…")` is the ONLY
  way to cast to int. Helper rejects empty / `+`/`-` / whitespace /
  decimal / hex / Unicode-digit input with `ValidationFailed`. When
  adding a new ID-bearing endpoint or an MCP tool that accepts an
  ID, declare the parameter as `str` and call `parse_int_id`. Never
  accept `int` directly from the wire (FastAPI's auto-coercion
  emits `422` with an inconsistent shape vs the rest of the API).

## File map (post-#61, on branch `feat/attachment-etag-if-range`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py attachments.py auth.py
    conditional.py                   # #59: ETag + If-None-Match + If-Range
    errors.py
    ids.py                           # #33: parse_int_id() boundary cast
    range_requests.py                # #54: parse_byte_range() + helpers
    messages.py sanitize.py search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/
      accounts.py                    # account_id: str + parse_int_id (#33)
      attachments.py                 # Range + 206/416 (#54) + force-DL (#32)
                                     #   + short-read WARNING (#58)
                                     #   + ETag / If-Range / 304 (#59)
      auth.py
      changes.py                     # since cursor uses parse_int_id (#33)
      messages.py                    # message_id: str + parse_int_id (#33)
      search.py version.py
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
  test_api_conditional.py            # 23 conditional-parser units (#59)
  test_api_ids.py                    # 11 parse_int_id unit tests (#33)
  test_api_range_requests.py         # 24 parse_byte_range unit tests (#54)
  test_serve_accounts_routes.py
  test_serve_messages_routes.py
  test_serve_changes_route.py
  test_api_attachments.py            # #32 filename helper tests
  test_serve_attachments_routes.py   # #32 force-download + #54 Range
                                     #   + #58 short-read WARNING tests
                                     # NB: 587 lines — split candidate
  test_serve_attachments_conditional.py  # #59 ETag + If-None-Match + If-Range
  test_daemon_pool.py                # #37 pool-sizing contract
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of conditional-GET session. Single commit `fc7c2a0` on
`feat/attachment-etag-if-range`; PR #61 open. Merge it, then either
do the **test-file split** (housekeeping — split #54+#58 blocks out
of `test_serve_attachments_routes.py`) or pick the next open issue.
