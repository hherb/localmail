# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-20 (post-session).** PR **#63**
> (`perf(serve): skip filename/file-open work on conditional GET 304
> (closes #62)`) opened against `main` on branch
> `feat/304-skip-file-open`, head commit **`3343094`**. Awaiting CI +
> merge — no review feedback at handoff time. Test surface on the
> branch: **653 passing** (`main` baseline 647 + 6 new). mypy clean
> (no new errors; 4 pre-existing parser.py errors carried forward
> from `main`, unrelated to this work).
>
> Branch `feat/304-skip-file-open` exists locally + on origin; do not
> delete until #63 is merged. Working tree clean otherwise.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What's open this session

| SHA | What |
|---|---|
| `3343094` (PR #63, open) | `perf(serve): skip filename/file-open work on conditional GET 304 (closes #62)` — adds a cheap DB-only probe `get_attachment_blob_info(conn, sha256, *, allowed_account_ids) -> (mime, size)` and reorders `stream_blob`'s work so the conditional check fires **before** the file open and JSONB filename scan. ACL still runs first inside the probe — a caller without a grant still sees 404, never 304, even when their `If-None-Match` would otherwise satisfy. 6 new tests landed: 4 unit + 2 route-level integration. |

### Issues resolved this session

- **#62** addressed by PR #63 (still open). Both acceptance criteria
  covered: the 304 short-circuit no longer invokes
  `open_attachment_bytes` or `get_attachment_filename`, and the
  ACL→precondition ordering keeps 404 winning over 304 for
  unauthorised callers.

### Concrete deliverables (commit `3343094`)

- **New** [`get_attachment_blob_info`](src/localmail/api/attachments.py)
  — DB-only ACL + existence probe. Returns `(mime_type, size_bytes)`
  without touching the filesystem or scanning carrying-message JSONB
  for a filename. Same ACL boundary as the rest of `api/attachments.py`
  (`_caller_can_read_blob` → `WHERE account_id = ANY(...)`).
- [`src/localmail/serve/routes/attachments.py`](src/localmail/serve/routes/attachments.py)
  — `stream_blob` reordered:
    1. ACL lookup (`allowed_account_ids`).
    2. `get_attachment_blob_info` → `(mime, size)` — cheap probe.
    3. `etag_for_sha256` + `if_none_match_satisfies` → return 304
       here, before any file IO.
    4. Only on the body-carrying path: `open_attachment_bytes` +
       `get_attachment_filename`.
  No behaviour change on 200 / 206 / 416 / If-Range / non-matching
  If-None-Match — same headers, same bytes, same MIME clamp. Single
  pool connection across all DB calls (no re-acquire on the
  body-carrying path).
- New tests in [`tests/test_api_attachments.py`](tests/test_api_attachments.py)
  (4 units, ~50 lines): success / NotFound / ACL-deny (both empty
  grants and wrong grants) / filesystem-untouched (probe still
  succeeds after the on-disk blob is unlinked). Plus
  `get_attachment_blob_info` joined the existing parametrized
  malformed-sha test that asserts `ValidationFailed`.
- New tests in [`tests/test_serve_attachments_conditional.py`](tests/test_serve_attachments_conditional.py)
  (2 integration tests, ~80 lines):
  - `test_304_does_not_call_open_attachment_bytes_or_filename` —
    monkeypatch-spies on both helpers, asserts the spy lists are
    empty after a 304 short-circuit. Direct regression guard for
    the #62 acceptance criterion.
  - `test_304_acl_denied_returns_404_not_304` — alice authenticated
    but **without** `grant_alice_all_accounts()`; matching
    `If-None-Match` returns 404. Closes the existence-oracle gap.
- [`CLAUDE.md`](CLAUDE.md) — new "304 short-circuit skips file-open
  + filename lookup (#62)" bullet next to the existing #32 / #54 /
  #58 / #59 download-invariant blocks. Documents:
  - The probe → conditional → expensive-IO ordering.
  - Why the probe enforces ACL (so 404 still wins over 304).
  - The two regression-guard tests, by name + by file.
  - The rule: when adding any new conditional-GET endpoint, follow
    the same ordering — never put expensive IO before the
    precondition check.

Test surface: 647 → 653 (+6 in this PR; baseline drift from the
prior handoff's 635 figure presumably accounts for tests that landed
on `main` after PR #61 — independently of this session's work).

## What's next

### 1. Merge PR #63

Once CI is green and review (if any) is addressed, squash-merge to
`main` and delete the branch locally + remotely. Carry the squash SHA
into the next handoff.

### 2. Pick the next piece of work

Top candidates (handoff-prioritised; #54 / #58 / #59 / #62 / #32-phase1
/ #33 / #37 / #9 closed by recent sessions):

- **Test-file split (no-feature housekeeping)** — `tests/
  test_serve_attachments_routes.py` is still 587 lines and was already
  flagged as a "split candidate." A natural split: pull the #54 (Range)
  and #58 (short-read) blocks into
  `tests/test_serve_attachments_streaming.py`. The #59 conditional
  tests are already in their own file; the new #62 tests landed in
  the conditional file too (no growth on the routes file). This is
  a small, low-risk PR.
- **#7** `Auth: IP-based / global login rate limiter` — well-scoped,
  clear acceptance criteria, ships before the GUI starts hitting the
  login route hard.
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
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.

**Recommendation**: do **the test-file split** next. It's a true
1-commit housekeeping PR (~150 lines moved + import adjustments) with
no scope creep, and it unblocks the next feature touching the
attachment route. Alternative: **#7** (IP rate limiter on login) is a
small feature with clean acceptance criteria.

## Open decisions & risks (carried forward)

1. **The probe duplicates `attachment_blobs` SELECT logic** with
   `get_attachment_metadata`. Both functions run identical SQL
   (`SELECT mime_type, size_bytes FROM attachment_blobs WHERE
   sha256 = %s`) and the same `_caller_can_read_blob` gate. Kept
   separate on purpose — `get_attachment_metadata` returns a dict
   for the `/v1/attachments/{sha256}/metadata` JSON path, the new
   probe returns a tuple for the streaming route. If a future
   refactor wants to unify, factor out a single `_lookup_blob`
   internal helper and have both call it — but two near-identical
   thin functions is fine for now and avoids over-design.

2. **The probe re-implements the ACL check** rather than threading
   through an "ACL already passed" flag. This is intentional
   defense-in-depth: every public function in `api/attachments.py`
   enforces ACL at the SQL boundary, with no "trust me" path. Same
   reason `get_attachment_metadata` and `open_attachment_bytes`
   each call `_caller_can_read_blob` independently.

3. **ACL is checked once per request** — `allowed_account_ids` is
   resolved once and reused for the probe + (on the body-carrying
   path) `open_attachment_bytes` + `get_attachment_filename`. Each
   of those still runs its own SQL ACL check, so the request pays
   for up to three identical EXISTS predicates on the 200 path. The
   GIN index from migration 0013 keeps each one cheap. Could be
   collapsed to one `_caller_can_read_blob` call if profiling shows
   it matters.

4. **304 carries `ETag` only (#59).** Per RFC 9110 §15.4.5, "a 304
   response MUST NOT generate representation metadata other than
   Content-Location, ETag, Vary, Cache-Control, Expires" — we emit
   ETag and nothing else. Notably: no Content-Disposition, no
   Accept-Ranges, no Content-Length on 304. Unchanged this session;
   regression-guarded by
   `test_if_none_match_with_current_etag_returns_304`.

5. **If-Range with HTTP-date always fails (#59).** Carried forward.

6. **Short-read response shape is unchanged (#58).** Carried forward.

7. **`bytes=0-<huge>` returns 206 with end-clamped, not 416** —
   carried forward from #54. Per RFC 9110 §14.1.2 end-past-EOF gets
   clamped to size-1.

8. **Multi-range falls through to 200, not 416 or
   `multipart/byteranges`.** Carried forward.

9. **`Accept-Ranges: bytes` is always advertised** on 200 / 206 / 416.
   On 304 we deliberately do not include it (§15.4.5 minimalism).

10. **`parse_int_id` from PR #55 — leading zeros are accepted but
    `"01"` resolves to `1`.** Carried forward.

11. **MIME clamp list (from PR #53) is small on purpose.** Only
    actively script-executable MIMEs are clamped to octet-stream.

12. **`rrf_k=60` is the centre of a flat plateau** (#35).

13. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
    `main` still need triage. Independent of this session's work.
    Run `gh api repos/hherb/localmail/vulnerability-alerts` to
    confirm the count hasn't drifted.

14. **`websockets.legacy` DeprecationWarning** (#25) still fires
    during `test_e2e_serve.py`. Pre-existing; upstream uvicorn
    blocker.

15. **Pre-#41 single-operator upgrade.** Anyone running a pre-
    `4e2e2f1` build with a created API user will see empty
    `/v1/accounts` until they run `localmail grant-account USERNAME
    <each-account>`.

16. **`tests/test_serve_attachments_routes.py` is still 587 lines.**
    The new #62 tests went into `test_serve_attachments_conditional.py`,
    so the routes-tests file did NOT grow this session. Still a soft
    split candidate — see "What's next" above.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin
git status                                 # working tree clean

# If PR #63 is still open, switch into it.
git checkout feat/304-skip-file-open
gh pr view 63                              # check CI + review

# After PR #63 is merged:
git checkout main
git pull
git branch -d feat/304-skip-file-open
git push origin :feat/304-skip-file-open

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 653 passed on branch / post-merge
unset VIRTUAL_ENV && uv run mypy src/localmail  # 4 pre-existing parser.py errors

# Pick the next piece of work.
gh issue list --state open --limit 40
gh issue view 7                            # IP rate limiter on login
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  picks the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0018 are additive.** Re-running `init-db` on an
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
- **extract_worker transient classification**: an exception is
  transient iff `isinstance(e, TransientExtractorError)` OR an instance
  of `(ConnectionError, TimeoutError, MemoryError)` appears anywhere in
  its `__cause__` / `__context__` chain.
- **`<a href>` deny schemes** (#48): `_HREF_DENY_SCHEMES = ("cid:",
  "data:")`.
- **RRF fusion is robust but un-tuned against production data** (#35).
- **`body_lang` worker index** (#40): the lang-detect claim query
  needs `messages_body_lang_pending_idx`.
- **Daemon pool sizing** (#37): all daemon threads share `Daemon.pool`.
  Its `max_size` is auto-computed via `db.compute_daemon_pool_size(...)`
  unless `DaemonConfig.pool_max_size` is set.
- **Attachment download invariants** (#32 phase 1 + #54 phase 2 +
  #58 phase 3 + #59 phase 4 + #62 phase 5): `/v1/attachments/{sha256}`
  ALWAYS emits `Content-Disposition: attachment` with both `filename=`
  (ASCII-sanitised) and `filename*=UTF-8''...` (percent-encoded
  original). Stored mime is clamped to `application/octet-stream` if
  it appears in `_INLINE_RISKY_MIMES`. `Accept-Ranges: bytes` and
  `ETag: "<sha>"` are set on every 200 / 206 / 416 response. A
  short-read against the on-disk blob (DB size > file size) emits a
  `WARNING` on the `localmail.serve` logger. A conditional GET
  (`If-None-Match` matches) returns 304 with `ETag` only — no body, no
  Content-Disposition, no Accept-Ranges, no Content-Length (§15.4.5).
  **The 304 short-circuit (#62) does NOT call `open_attachment_bytes`
  or `get_attachment_filename`** — the cheap DB-only probe
  `get_attachment_blob_info` enforces ACL and surfaces `(mime, size)`
  for the conditional check. ACL→precondition ordering is preserved:
  an unauthorised caller still sees 404, never 304.
- **Range parsing boundary** (#54): `localmail.api.range_requests.
  parse_byte_range` is the only path that interprets `Range: bytes=…`.
- **Conditional parsing boundary** (#59): `localmail.api.conditional`
  is the only path that interprets `If-None-Match` and `If-Range`.
  `etag_for_sha256` is the only path that builds an ETag header
  value.
- **Probe-then-condition boundary** (#62): for any new conditional-GET
  endpoint, the order is **ACL+probe → precondition → expensive IO**.
  The probe MUST enforce ACL so 404 still wins over 304 for
  unauthorised callers. The probe MUST NOT touch the filesystem or
  do JSONB scans — those are the costs the conditional short-circuit
  exists to avoid.
- **Streaming WARNING contract** (#58): any new streaming endpoint
  that advertises a `Content-Length` MUST also count bytes yielded
  and call `_log_truncation()` when the source runs short.
- **ID-typing boundary** (#33): path and query parameters bearing
  entity IDs are typed `str` on the route handler signature, and
  `localmail.api.ids.parse_int_id(value, field="…")` is the ONLY
  way to cast to int.

## File map (as of branch HEAD `3343094`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py
    attachments.py                   # +get_attachment_blob_info() (#62 probe)
    auth.py
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
                                     #   + 304 probe-first (#62)
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
migrations/                          # 0001 … 0018_messages_date_received_internaldate.sql
tests/
  acceptance/
    run_recall_eval.py               # Phase 1 multilingual gate
    run_attachment_eval.py           # Phase 2 attachment gate
    run_rrf_k_sweep.py               # #35 sweep harness
  test_api_attachments.py            # +4 get_attachment_blob_info units (#62)
  test_api_conditional.py            # 23 conditional-parser units (#59)
  test_api_ids.py                    # 11 parse_int_id unit tests (#33)
  test_api_range_requests.py         # 24 parse_byte_range unit tests (#54)
  test_serve_accounts_routes.py
  test_serve_messages_routes.py
  test_serve_changes_route.py
  test_serve_attachments_routes.py   # #32 + #54 + #58 (587 lines, split candidate)
  test_serve_attachments_conditional.py  # #59 ETag/If-Range/If-None-Match
                                     #   + #62 304 probe-skip + ACL-vs-304 (2 new)
  test_daemon_pool.py                # #37 pool-sizing contract
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
  2026-05-20T1300-conditional-get-304-skip-pr-63.md   # this session's snapshot
NEXT_SESSION.md                      # this file (post-session)
```

End of 304-probe-skip session. PR #63 open against `main`
(`3343094`). Branch `feat/304-skip-file-open` is alive on local + remote
until merge. Next: merge #63, then either the **test-file split** (no-
feature housekeeping) or pick an open issue — **#7** (IP rate limiter)
is the most clearly-scoped feature in the queue.
