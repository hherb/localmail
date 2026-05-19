# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-19 (end of session).** PR **#55** (ID typing
> on the wire, closes #33) merged at the start of this session as
> squash commit `22e1f35`.
>
> This session shipped PR **#57** (`feat(serve): HTTP Range support on
> attachment endpoint, closes #54`) — branch
> `feat/attachment-range-support`, single commit `de1ceaf`, **585
> tests pass** (552 baseline + 33 new), mypy clean on touched files.
> PR #57 **OPEN** at session close.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `de1ceaf` | `feat(serve): HTTP Range support on attachment endpoint (closes #54)` — new pure `localmail.api.range_requests` (`parse_byte_range`, `ByteRange`, `UnsatisfiableRange`, header helpers) with 24 unit tests; `/v1/attachments/{sha256}` now advertises `Accept-Ranges: bytes` and emits 206 Partial Content / 416 Range Not Satisfiable per RFC 9110 §14.1; route uses `.seek(start)` + bounded chunked read (no full-blob slurp); 200 / 206 / 416 all carry the #32 `Content-Disposition: attachment` + MIME clamp; CLAUDE.md documents the new invariant. |

PR **#57** opened against `main` (status: **OPEN** at session close).

### Issue closed (by PR #57)

- **#54** — `api: attachment streaming — Range request support (phase
  2 of #32)`. All acceptance criteria from the issue are covered by
  tests. **Documented deviation from issue body**: `bytes=0-<huge>`
  with start in range returns **206 with end clamped to size-1** (RFC
  9110 §14.1.2 explicit guidance, and what every mainstream HTTP
  server + curl + browsers expect), not 416 as the issue body suggested.
  416 is reserved for *start*-past-EOF. PR description calls this out.

### Concrete deliverables in PR #57

- New module
  [`src/localmail/api/range_requests.py`](src/localmail/api/range_requests.py)
  — pure parser, no IO, no FastAPI:
  - `parse_byte_range(header: str | None, total_size: int) -> ByteRange | None`
    — `None` for absent/invalid/multi-range (caller serves 200);
    `ByteRange` for satisfiable; `UnsatisfiableRange` exception for
    start past EOF / suffix-0 / empty resource.
  - `content_range_header(byte_range, total_size)` and
    `unsatisfiable_content_range(total_size)` helpers — single source
    of truth for the wire format.
  - `RANGE_UNIT = "bytes"`, `RANGE_PREFIX = "bytes="` — module-level
    constants (no magic strings inline in the parser).
- [`src/localmail/serve/routes/attachments.py`](src/localmail/serve/routes/attachments.py)
  — `stream_blob` split into three branches:
  - **No / unparseable / multi-range header** → 200 + `Accept-Ranges:
    bytes`.
  - **Satisfiable byte range** → 206 with `Content-Range`,
    `Content-Length` = slice length, and the streaming generator
    `_stream_range` (which does one `.seek(start)` and reads in
    `_CHUNK`-sized blocks bounded by `remaining`).
  - **Unsatisfiable** → 416 with `Content-Range: bytes */N`. fp closed
    eagerly on this path.
  - **`_HTTP_PARTIAL_CONTENT = 206`** and
    **`_HTTP_RANGE_NOT_SATISFIABLE = 416`** module constants — no
    magic numbers in the route body.
- [`tests/test_api_range_requests.py`](tests/test_api_range_requests.py)
  — 24 unit tests covering: absent header, non-`bytes` unit, missing
  `=`, empty spec, no `-`, bare `-`, first N bytes, open-ended
  (`bytes=N-`), suffix (`bytes=-N`), suffix > size (whole file),
  end past EOF (clamped), start past EOF (416), open-ended start
  past EOF (416), suffix-0 (416), negative suffix (416), non-numeric
  (200 fall-through), end < start (200 fall-through), multi-range
  (200 fall-through), empty resource (416), `ByteRange.length`
  property, `Content-Range` formats, `bytes=0-` whole-file shortcut,
  `bytes=5-5` single byte.
- [`tests/test_serve_attachments_routes.py`](tests/test_serve_attachments_routes.py)
  — 9 new route-level tests:
  - `test_stream_attachment_advertises_accept_ranges_bytes` (renamed
    from `_accept_ranges_none`).
  - 5 happy-path tests for closed / open-ended / suffix / end-past-EOF
    clamp / start-past-EOF.
  - `test_malformed_range_falls_through_to_200` and
    `test_multi_range_falls_through_to_200`.
  - `test_range_request_preserves_content_disposition_and_mime_clamp`
    (defense in depth: a 206 over `text/html` still gets clamped to
    `application/octet-stream` + `Content-Disposition: attachment`).
  - `test_416_response_preserves_content_disposition` (proxy/client
    can't be tricked into rendering a 416 body inline).
- [`CLAUDE.md`](CLAUDE.md) — new "Range support (#54, phase 2 of #32)"
  invariant block next to the existing #32 phase 1 block. Documents
  the seek-then-bounded-read streaming contract and that all of
  200 / 206 / 416 carry the same `Content-Disposition` + MIME clamp.

Test surface: 552 → 585 (+33 new tests, 24 parser + 9 route).

## What's next — concrete acceptance criteria

PR #57 needs to merge first. Once it does:

### 1. Merge PR #57 and clean up

```bash
gh pr view 57                       # confirm CI green
gh pr merge 57 --squash --delete-branch
git checkout main && git pull
```

### 2. Pick the next open issue

Top candidates (handoff-prioritised; #33 / #37 / #9 / #32-phase1 /
#54 closed by this and previous sessions):

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

1. **`bytes=0-<huge>` returns 206 with end-clamped, not 416** — the
   issue body suggested 416 for "past end", but RFC 9110 §14.1.2
   explicitly says end-past-EOF gets clamped to size-1 and the range
   is interpreted as "remainder of the representation". Every
   mainstream HTTP server (nginx, Apache, S3, GCS, FastAPI's own
   `FileResponse`) does the same; 416 is reserved for *start*-past-EOF.
   PR #57 description calls this out. If a downstream consumer ever
   complains about getting 206 + clamped slice instead of 416, they
   are probably testing against a non-conforming server; point them
   at the RFC.

2. **Multi-range falls through to 200, not 416 or `multipart/byteranges`.**
   The issue offered both options; we picked 200 because:
   (a) single-range covers PDF/video seek + connection resume, which
   is all the GUI needs;
   (b) `multipart/byteranges` adds non-trivial code (boundary
   negotiation, per-part headers) for zero observed clients; and
   (c) RFC 9110 §14.1.2's permissive clause explicitly allows servers
   to ignore Range syntax they don't support. If a real-world client
   ever wants multi-range, it'll be obvious in access logs and we can
   add it then.

3. **`Accept-Ranges: bytes` is now always advertised** — including
   on `/v1/attachments/{sha256}/text` if anyone reaches that endpoint
   via the wrong path. Currently it isn't; the `/text` endpoint
   returns JSON via its own handler that does *not* advertise Range
   support (correct — Range over JSON is nonsensical). If someone
   adds a new streaming endpoint, they get `Accept-Ranges: bytes`
   only if they explicitly set it.

4. **`Accept-Ranges: none` is gone.** Anyone who built tooling against
   the old phase-1 contract (which explicitly set `Accept-Ranges:
   none`) and asserted on it will now break. None known in tree
   (Tauri GUI doesn't assert on this header), but anyone running an
   external scraper against the API may see different behaviour. This
   is the intended tightening — the whole point of #54 is to flip
   `none` to `bytes`.

5. **`parse_int_id` from PR #55 — leading zeros are accepted but
   `"01"` resolves to `1`.** Two stable encodings of the same int
   are therefore both valid path params. Unchanged from previous
   handoff. Document the canonical form in the OpenAPI spec when
   we add one.

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

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # feat/attachment-range-support if PR #57 not yet merged

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 585 passed

# Merge PR #57.
gh pr view 57                               # confirm CI green
gh pr merge 57 --squash --delete-branch
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
- **Attachment download invariants** (#32 phase 1 + #54 phase 2):
  `/v1/attachments/{sha256}` ALWAYS emits `Content-Disposition:
  attachment` with both `filename=` (ASCII-sanitised) and
  `filename*=UTF-8''...` (percent-encoded original). Stored mime is
  clamped to `application/octet-stream` if it appears in
  `_INLINE_RISKY_MIMES`. `Accept-Ranges: bytes` is set on every
  response (was `none` pre-#54). All three invariants apply to
  200 / 206 / 416 — the test suite enforces this. The filename helper
  picks the earliest carrying message by `messages.id`; if you change
  that pick, update
  `test_get_attachment_filename_prefers_first_carrying_message`.
- **Range parsing boundary** (#54): `localmail.api.range_requests.
  parse_byte_range` is the only path that interprets `Range: bytes=…`.
  `None` return → caller serves 200; `UnsatisfiableRange` exception →
  caller serves 416 with `unsatisfiable_content_range(size)`. Don't
  parse Range inline in route handlers — extend the pure module so
  new transports (MCP, etc.) get the same behaviour for free.
- **ID-typing boundary** (#33): path and query parameters bearing
  entity IDs are typed `str` on the route handler signature, and
  `localmail.api.ids.parse_int_id(value, field="…")` is the ONLY
  way to cast to int. Helper rejects empty / `+`/`-` / whitespace /
  decimal / hex / Unicode-digit input with `ValidationFailed`. When
  adding a new ID-bearing endpoint or an MCP tool that accepts an
  ID, declare the parameter as `str` and call `parse_int_id`. Never
  accept `int` directly from the wire (FastAPI's auto-coercion
  emits `422` with an inconsistent shape vs the rest of the API).

## File map (post-#57, on branch `feat/attachment-range-support`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py attachments.py auth.py errors.py
    ids.py                           # #33: parse_int_id() boundary cast
    range_requests.py                # NEW (#54): parse_byte_range() + helpers
    messages.py sanitize.py search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/
      accounts.py                    # account_id: str + parse_int_id (#33)
      attachments.py                 # Range + 206/416 (#54) + #32 invariants
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
  test_api_range_requests.py         # NEW: 24 parse_byte_range unit tests (#54)
  test_serve_accounts_routes.py      # +1 malformed-id test (#33)
  test_serve_messages_routes.py      # +3 malformed-id / string-id tests (#33)
  test_serve_changes_route.py        # existing 400 test (#33)
  test_api_attachments.py            # #32 filename helper tests
  test_serve_attachments_routes.py   # #32 force-download + #54 Range tests
  test_daemon_pool.py                # #37 pool-sizing contract
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of Range-support session. Single commit `de1ceaf` on
`feat/attachment-range-support`; PR #57 open. Merge it, then pick
**#38** (`/v1/changes` semantics decision) — best decided after
observing what the GUI does in production, but well-scoped if you
want to commit to a direction.
