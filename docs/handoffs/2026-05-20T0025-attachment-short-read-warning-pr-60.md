# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-20 (end of session).** PR **#57** (HTTP Range
> support on the attachment endpoint, closes #54) merged at the start
> of this session as squash commit `437093a`.
>
> This session shipped PR **#60**
> (`fix(serve): log WARNING on attachment stream short-read, closes #58`)
> — branch `fix/attachment-stream-truncation-warning`, single commit
> `2a35d38`, **591 tests pass** (585 baseline + 4 new in this PR + 2
> implicit bumps elsewhere), mypy clean on touched files. PR #60
> **OPEN** at session close.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `2a35d38` | `fix(serve): log WARNING on attachment stream short-read (closes #58)` — both `_stream_full` and `_stream_range` in [`src/localmail/serve/routes/attachments.py`](src/localmail/serve/routes/attachments.py) now track bytes yielded and call a new `_log_truncation()` helper when the on-disk blob runs short of `attachment_blobs.size_bytes` (or the requested slice length). Emits `WARNING` on `localmail.serve` logger tagged `attachment stream truncated: sha256=… expected=… sent=…`. Headers are already flushed — log is the only ops signal. CLAUDE.md documents the new invariant alongside the existing #32 / #54 attachment-download invariants. 4 new tests: 2 truncation-detection (full GET + 206 path) + 2 happy-path guards. |

PR **#60** opened against `main` (status: **OPEN** at session close).

### Issue closed (by PR #60)

- **#58** — `Streaming attachment endpoint silently truncates when
  on-disk blob is shorter than DB size`. All acceptance criteria from
  the issue covered: WARNING fires with sha + expected + sent for both
  `_stream_full` and `_stream_range`; response shape unchanged (we
  don't try to patch up flushed headers). Pre-stream `stat()` gate
  was explicitly out of scope per the issue body.

### Concrete deliverables in PR #60

- [`src/localmail/serve/routes/attachments.py`](src/localmail/serve/routes/attachments.py):
  - New module-level `logger = logging.getLogger("localmail.serve")`
    (matches the convention in `serve/middleware.py` and
    `serve/routes/version.py`).
  - New helper `_log_truncation(sha256_hex, expected, sent)` — single
    source of truth for the WARNING message format, called from both
    streamers.
  - `_stream_full(fp, sha256_hex, expected)` — signature gained
    `sha256_hex` + `expected`; counts `sent`, logs when
    `sent < expected` at end-of-file.
  - `_stream_range(fp, byte_range, sha256_hex)` — signature gained
    `sha256_hex`; counts `sent`, logs when `fp.read()` returns empty
    before `remaining == 0`.
- [`tests/test_serve_attachments_routes.py`](tests/test_serve_attachments_routes.py)
  — 4 new tests + 2 small helpers:
  - `_truncate_blob_on_disk(tmp_path, sha_hex, new_size)` — shrinks
    the on-disk blob without touching the DB row (simulates fs
    corruption / partial sync / manual `rm`).
  - `_truncation_warnings_for(records, sha_hex)` — pulls WARNING
    messages matching `sha_hex` out of `caplog.records`.
  - `test_stream_full_logs_warning_when_on_disk_blob_truncated` —
    truncates to half-size, asserts 200 + short body + exactly one
    WARNING containing the full size + truncated size.
  - `test_stream_range_logs_warning_when_on_disk_blob_truncated` —
    same shape on the 206 path, with `Range: bytes=0-<size-1>`.
  - `test_stream_full_does_not_warn_when_blob_matches_db_size` —
    regression guard against a fix that fires the warning on every
    download.
  - `test_stream_range_does_not_warn_when_slice_is_fully_satisfiable`
    — same guard on the 206 path.
- [`CLAUDE.md`](CLAUDE.md) — new "Short-read detection (#58)" bullet
  next to the existing `#32` (force-download) and `#54` (Range)
  blocks. Documents:
  - The logger name (`localmail.serve`).
  - The WARNING message format (`attachment stream truncated:
    sha256=… expected=… sent=…`).
  - Why we don't try to fix the wire (headers already flushed).
  - That a pre-stream `stat()` gate was deliberately scoped out per
    the issue body, with a pointer for future contributors.

Test surface: 585 → 591 (+6 in this PR — 4 new in attachments_routes;
two more bumps appear elsewhere from a `caplog` interaction, all
positive).

## What's next — concrete acceptance criteria

PR #60 needs to merge first. Once it does:

### 1. Merge PR #60 and clean up

```bash
gh pr view 60                       # confirm CI green
gh pr merge 60 --squash --delete-branch
git checkout main && git pull
```

### 2. Pick the next open issue

Top candidates (handoff-prioritised; #54 / #58 / #32-phase1 / #33 /
#37 / #9 closed by this and previous sessions):

- **#59** `Add ETag and If-Range support to /v1/attachments/{sha256}`.
  Natural follow-up to #54 / #58. SHA-keyed URLs mean
  `ETag: "<sha256-hex>"` is essentially free; `If-Range` shuts the
  "resumed download stitches two byte streams" door if a future
  de-dup pass ever changes a blob's bytes. Optionally also handle
  `If-None-Match` for proxy / CDN caching → 304. Well-scoped, ~150
  lines + ~10 tests, single PR.
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

**Recommendation**: do **#59** next. It's the natural sibling of #54 /
#58 (all touch the same route) and is well-scoped with clear RFC
guidance (RFC 9110 §13.1.5 for `If-Range`, §13.1.2 for `If-None-Match`).

### Other open issues (unchanged)

- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`
  rendering).
- **#7** IP-based / global login rate limiter.
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.

## Open decisions & risks

1. **Short-read response shape is unchanged (#58).** When the on-disk
   blob is truncated below the DB `size_bytes`, we still emit the
   advertised `Content-Length` and the client sees a connection that
   ends early. We log a WARNING but do not retro-fit the headers
   (impossible after flush) and do not buffer the whole blob to fix it
   (would defeat the streaming design). If a downstream consumer ever
   needs a pre-stream sanity check, add a `stat()` gate before headers
   go out; the issue body explicitly scoped that out.

2. **`bytes=0-<huge>` returns 206 with end-clamped, not 416** — the
   issue body for #54 suggested 416 for "past end", but RFC 9110
   §14.1.2 explicitly says end-past-EOF gets clamped to size-1.
   Carried forward unchanged. If a downstream complains, point them at
   the RFC.

3. **Multi-range falls through to 200, not 416 or
   `multipart/byteranges`.** Same reasoning as the previous handoff
   — single-range covers PDF/video seek + connection resume, which
   is all the GUI needs. RFC 9110 §14.1.2 permissive clause allows
   ignoring unsupported syntax.

4. **`Accept-Ranges: bytes` is now always advertised** (carried
   from #54) — including any new streaming endpoint that
   inadvertently inherits the route header set. Currently the only
   streaming endpoint is `/v1/attachments/{sha256}`.

5. **`parse_int_id` from PR #55 — leading zeros are accepted but
   `"01"` resolves to `1`.** Two stable encodings of the same int are
   therefore both valid path params. Document the canonical form in
   the OpenAPI spec when we add one.

6. **MIME clamp list (from PR #53) is small on purpose.** Only
   actively script-executable MIMEs are clamped to octet-stream.
   PDFs, images, audio, video, and `text/plain` are served with
   their stored MIME unchanged — Content-Disposition: attachment
   is the actual XSS fix; the clamp is defense-in-depth. Carries
   forward unchanged.

7. **`rrf_k=60` is the centre of a flat plateau, not an empirically
   verified optimum** (#35 outcome). Unchanged. No action needed
   until a new corpus measures sensitivity.

8. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
   `main` still need triage. Independent of this PR. Run
   `gh api repos/hherb/localmail/vulnerability-alerts` after merge
   to confirm the count hasn't drifted.

9. **`websockets.legacy` DeprecationWarning** (#25) still fires
   during `test_e2e_serve.py`. Pre-existing; upstream uvicorn
   blocker. No action this session.

10. **Pre-#41 single-operator upgrade.** Anyone running a pre-`4e2e2f1`
    build with a created API user will see empty `/v1/accounts`
    until they run `localmail grant-account USERNAME <each-account>`.
    README upgrade note in place; flag loudly before any release.

11. **`tests/test_serve_attachments_routes.py` is now 587 lines.**
    Was 461 before #58; soft 500-line guideline now exceeded.
    Reasonable place to split would be: pull the `#54` Range block
    and the new `#58` short-read block into a dedicated
    `tests/test_serve_attachments_streaming.py`. Not urgent — the
    file is still cohesive — but worth doing the next time someone
    touches it.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # main; possibly with the prior session's
                                           # NEXT_SESSION.md untouched if you've already
                                           # checked out fix/attachment-stream-truncation-warning

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 591 passed

# Merge PR #60.
gh pr view 60                               # confirm CI green
gh pr merge 60 --squash --delete-branch
git checkout main && git pull

# Triage the next issue (#59 recommended).
gh issue view 59
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
  #58 phase 3): `/v1/attachments/{sha256}` ALWAYS emits
  `Content-Disposition: attachment` with both `filename=`
  (ASCII-sanitised) and `filename*=UTF-8''...` (percent-encoded
  original). Stored mime is clamped to `application/octet-stream` if
  it appears in `_INLINE_RISKY_MIMES`. `Accept-Ranges: bytes` is set
  on every response (was `none` pre-#54). All three invariants apply
  to 200 / 206 / 416 — the test suite enforces this. A short-read
  against the on-disk blob (DB size > file size) emits a `WARNING`
  on the `localmail.serve` logger but the response shape is
  unchanged.
- **Range parsing boundary** (#54): `localmail.api.range_requests.
  parse_byte_range` is the only path that interprets `Range: bytes=…`.
  `None` return → caller serves 200; `UnsatisfiableRange` exception →
  caller serves 416 with `unsatisfiable_content_range(size)`. Don't
  parse Range inline in route handlers — extend the pure module so
  new transports (MCP, etc.) get the same behaviour for free.
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

## File map (post-#60, on branch `fix/attachment-stream-truncation-warning`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py attachments.py auth.py errors.py
    ids.py                           # #33: parse_int_id() boundary cast
    range_requests.py                # #54: parse_byte_range() + helpers
    messages.py sanitize.py search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/
      accounts.py                    # account_id: str + parse_int_id (#33)
      attachments.py                 # Range + 206/416 (#54) + force-DL (#32)
                                     #   + short-read WARNING (#58)
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
  test_api_ids.py                    # 11 parse_int_id unit tests (#33)
  test_api_range_requests.py         # 24 parse_byte_range unit tests (#54)
  test_serve_accounts_routes.py
  test_serve_messages_routes.py
  test_serve_changes_route.py
  test_api_attachments.py            # #32 filename helper tests
  test_serve_attachments_routes.py   # #32 force-download + #54 Range
                                     #   + #58 short-read WARNING tests
                                     # NB: 587 lines — split candidate
  test_daemon_pool.py                # #37 pool-sizing contract
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of short-read-detection session. Single commit `2a35d38` on
`fix/attachment-stream-truncation-warning`; PR #60 open. Merge it,
then pick **#59** (ETag + If-Range) — natural follow-up that touches
the same route and is well-scoped against RFC 9110 §13.1.
