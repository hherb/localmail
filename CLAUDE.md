# CLAUDE.md

Guidance for Claude Code sessions working in this repo. See [README.md](README.md)
for the end-user view.

## What this is

`localmail` mirrors one or more IMAP accounts (password or Gmail OAuth2) into a
PostgreSQL database. **Read-only with respect to upstream**: localmail never
deletes, modifies, or sends mail. Downstream agents consume the DB and the
attachment tree without touching IMAP.

## Stack (locked in)

- Python ≥ 3.12, managed by `uv` (`uv sync`, `uv run …`).
- Postgres access: `psycopg` v3 + raw SQL + numbered `.sql` files in
  `migrations/`. **No ORM.** Migrations are tracked in `schema_migrations`.
- IMAP: `imapclient` (sync, blocking). Gmail OAuth2 uses XOAUTH2 via
  `google-auth` + `google-auth-oauthlib`.
- Secrets: `keyring` (service `"localmail"`, username = `<account.name>` for
  passwords, `<account.name>:refresh` for OAuth refresh tokens). Cross-platform:
  macOS Keychain on darwin, Secret Service on Linux.
- Config: TOML, validated by `pydantic` v2.
- CLI: `click`.
- Tests: `pytest` (in-memory `keyring` backend; real Postgres at
  `LOCALMAIL_TEST_DSN`, defaults to a separate `localmail_test` database so
  tests can't clobber the live archive).

## Commands

```bash
uv sync                          # install deps
uv run pytest                    # full test suite (skips DB tests if PG unreachable)
uv run localmail init-db         # apply pending migrations
uv run localmail list-accounts   # show config'd accounts and whether a secret is stored
uv run localmail add-account N   # store password for account N (must exist in config.toml)
uv run localmail remove-account N  # drop stored secrets for account N
uv run localmail oauth-login N   # Gmail desktop OAuth flow → refresh token in keyring
uv run localmail sync [--account N] [--limit-per-folder K]   # one-shot incremental sync
uv run localmail run             # foreground daemon (IDLE on INBOX + periodic poll)
uv run localmail list-failed [--account N] [--limit K]   # show messages sync skipped
uv run localmail retry-failed [--account N]    # re-attempt every failed message
uv run localmail extract-backfill              # one-shot extraction backfill for all blobs
uv run localmail lang-backfill                 # one-shot body_lang detection for existing rows
uv run localmail backfill-internal-date [--account N]  # IMAP INTERNALDATE for legacy rows
uv run localmail list-failed-extractions [--limit K]   # show blobs extraction skipped
uv run localmail retry-failed-extractions      # re-attempt every failed extraction
# search-status reports Phase 2 attachment_text/attachment_chunks counts and
# body_lang_populated / body_lang_pending
```

GUI server (Phase: gui-server):

```bash
uv run localmail add-api-user USERNAME       # create an API user (no grants by default)
uv run localmail list-api-users [--with-grants]
uv run localmail remove-api-user USERNAME
uv run localmail grant-account USERNAME ACCOUNT_NAME   # per-user ACL (migration 0016)
uv run localmail revoke-account USERNAME ACCOUNT_NAME
uv run localmail rotate-tls --cert PATH --key PATH
uv run localmail serve [--bind 127.0.0.1] [--port 8443] \
                       [--tls-cert PATH] [--tls-key PATH] [--no-tls]
```

Common gotcha when running ad-hoc commands: shells often have `VIRTUAL_ENV`
set to some other pyenv venv, which makes `uv run` warn and (with `--active`)
pick the wrong interpreter. Prefix with `unset VIRTUAL_ENV && …` to be safe.

## Layout

```
src/localmail/
  cli.py            # click entry point
  config.py         # pydantic models + TOML loader
  db.py             # connection pool + migration runner
  secrets.py        # keyring wrapper
  oauth_gmail.py    # OAuth2 desktop flow + token refresh
  imap_client.py    # open_connection() context manager (password / XOAUTH2)
  parser.py         # bytes -> ParsedMessage (pure; no IO; NUL-strip + empty->None)
  attachments.py    # write_attachments(conn, parsed, root) -> JSONB rows (content-addressable)
  sync.py           # upsert_*, process_one_message, sync_mailbox, sync_account,
                    #   record_failed_message, retry_failed_messages, folders_to_sync
  worker.py         # WorkerContext shared by daemon threads
  idle.py           # run_inbox_idle_loop, _one_inbox_session, _idle_step
  poller.py         # run_poll_loop, _one_poll_pass
  daemon.py         # Daemon class: signal handling, per-account thread spawn
  search/           # hybrid search subsystem (Phases 1 + 2)
    __init__.py     # public API: create_searcher, Searcher, SearchPage, SearchResult
    arms.py         # retrieval arms: arm_bm25_messages, arm_bm25_chunks, arm_vector_chunks, arm_vector_attachment_chunks
    chunking.py     # chunk_message() -> ChunkSpec list; chunk_attachment_text() -> ChunkSpec list
    embed_worker.py # run_embed_worker_once, run_embed_worker (background thread)
    embeddings.py   # FastEmbedBackend + EmbeddingBackend ABC
    extractor.py    # LightweightExtractor (11 formats) + ExtractorBackend ABC; DoclingExtractor via [extraction] extra
    extract_worker.py # run_extract_worker_once, run_extract_worker (background thread)
    lang_detect.py  # LinguaDetector + FixedDetector + run_lang_detect_pass for messages.body_lang
    page_cache.py   # in-process LRU cache for paginated result pools
    query.py        # parse_query() -> ParsedQuery, SearchFilters, filter DSL
    reranker.py     # FastEmbedReranker + Reranker ABC
    searcher.py     # Searcher orchestrator, rrf_fuse(), make_snippet(), SearchResult
migrations/         # 0001_init.sql … 0018_messages_date_received_internaldate.sql
tests/
  acceptance/       # standalone eval harnesses (run_recall_eval.py,
                    # run_attachment_eval.py, run_rrf_k_sweep.py)
  conftest.py       # memory_keyring fixture, db_dsn/db_conn fixtures
  _eml.py           # MIME fixture builders (no .eml files on disk)
  _fake_imap.py     # in-memory IMAP fake with IDLE support
  _multilingual_corpus.py  # synthetic 50-message corpus for multilingual eval
  fixtures/         # multilingual_queries.example.json
  test_*.py
config.example.toml
```

User-facing config lives at `~/.config/localmail/config.toml` (override with
`$LOCALMAIL_CONFIG` or `localmail --config PATH …`).

## Schema essentials

Tables: `accounts`, `mailboxes`, `messages`, `message_labels`,
`attachment_blobs`, `attachment_text`, `attachment_chunks`,
`failed_messages`, `failed_extractions`, `api_users`, `api_tokens`,
`user_accounts`, `schema_migrations`. Dedup model:

- **Messages — per-account, by `Message-Id`**: same Message-Id in INBOX + 3
  Gmail labels produces one `messages` row + four `message_labels` rows. The
  same Message-Id on a different account is a separate `messages` row
  (provenance preserved).
- **Messages — fallback when no Message-Id**: dedup by SHA-256 of the raw
  RFC822 bytes (`messages.raw_sha256`, partial unique index when
  `message_id IS NULL`).
- **Attachments — content-addressable, global**: identical bytes appear on
  disk and in `attachment_blobs` exactly once across the whole archive
  regardless of account/message. `messages.attachments` JSONB stores
  `[{"filename": "<original-name-from-this-email>", "sha256": "<hex>",
    "content_id": "<cid-without-brackets>"}, …]` — `content_id` is only
  present on inline parts (HTML bodies reference them via `cid:`), omitted
  otherwise. The original filename is preserved per-message so files can be
  restored with the names they had when received; the bytes, mime type, size,
  and on-disk path live on the `attachment_blobs` row.

On-disk path: `<attachments.root>/blobs/<aa>/<bb>/<full-sha256-hex>` (two-level
hex fan-out). The path is opaque — never derive filenames from it; always go
through the JSONB.

**Nullability**: only `raw_bytes`, `size_bytes`, `headers`, and `attachments`
are `NOT NULL` on `messages`. `subject`, `body_text`, `body_html`, `from_addr`,
`to_addrs`, etc. are all nullable — real mail occasionally lacks any of them.
The parser normalizes empty strings to NULL so `WHERE body_text IS NULL` is
the canonical "no body" query.

**Date columns** (`date_sent`, `date_received`, `internal_date`):
- `date_sent` — email header `Date:`. Sender-supplied, may be wrong/future,
  usually accurate. Nullable.
- `internal_date` — IMAP server INTERNALDATE (RFC 3501), "when this email
  arrived at the mailbox". Populated by `sync.py:upsert_message` on insert;
  legacy rows (pre-migration-0018) are NULL until backfilled via
  `localmail backfill-internal-date`. Nullable.
- `date_received` — local sync timestamp, `NOT NULL`. Not a meaningful
  "received" date; reflects "when localmail wrote this row". Used by
  `/v1/changes` as a safe-horizon filter (`< now() - changes_safe_horizon_s`)
  and for audit.

The canonical "show me recent mail" ordering is
`ORDER BY COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC`,
backed by the expression index `messages_recent_idx`. Used by both the
`/v1/changes` initial-fetch branch and the `Searcher` empty-query fallback
(`_list_recent_messages`).

Folder filtering supports `folder_allow`, `folder_deny`, and **`folder_deny_flags`**
(by RFC 6154 IMAP special-use flag, e.g. `\Trash`, `\Junk`, `\All`). Prefer
flag-based denial — it survives provider locales (`[Gmail]/Bin` vs `Trash`).

## Sync model

- One-shot via `localmail sync`: useful for cron and smoke testing.
  `--limit-per-folder K` caps how many UIDs are processed per mailbox per run;
  the next run resumes from `mailboxes.uidnext`.
- Daemon via `localmail run`: per account, **two threads** — one IDLE on INBOX,
  one periodic poll on every other folder. **All daemon threads share a single
  `psycopg_pool.ConnectionPool`** (`Daemon.pool`): IDLE + poll per account,
  plus the optional `embed_worker` and `extract_worker` threads. They
  coordinate via a `threading.Event` stop signal and reconnect with
  exponential backoff (1s → 60s cap) on failure. SIGTERM/SIGINT cleanly stop
  IDLE and join threads.

  Pool sizing: by default `compute_daemon_pool_size(...)` in `db.py` derives
  the cap from `(2 * n_accounts) + workers + headroom`, floored at
  `POOL_BASELINE_MIN`. Set `daemon.pool_max_size` in `config.toml` to
  override for tight Postgres `max_connections` budgets or higher concurrency.
  The chosen value is logged at startup ("daemon pool sizing: max_size=…").

`sync_mailbox` checkpoints `mailboxes.uidnext` after each 50-message batch, so
a crash mid-run loses at most one batch of progress. Re-running is safe — the
existing-id check + `ON CONFLICT DO NOTHING` make inserts idempotent.

### Failure handling (poison-pill messages)

Per-message work runs inside a Postgres `SAVEPOINT msg` so a single bad row
(unexpected encoding, NUL byte the parser missed, etc.) only loses itself,
not the surrounding 49 messages in the batch. On exception:

1. `ROLLBACK TO SAVEPOINT msg` — discards just this message's writes.
2. `record_failed_message` inserts the full RFC822 bytes + error class +
   message + traceback into `failed_messages` (its own nested SAVEPOINT so a
   logging failure can't kill the outer transaction). Re-failures upsert and
   bump `retry_count`.
3. `mailboxes.uidnext` still advances past the failed UID — we don't get
   stuck retrying the same poison pill on every run.

Recovery flow: fix the parser bug, run `localmail retry-failed`. The retry
path calls the same `process_one_message` as live sync, so any fix that works
for new messages also works for the backlog.

The parser itself does two pre-emptive sanitizations to keep poison pills
rare: NUL bytes are stripped from every text field (Postgres `TEXT` rejects
them), and attachment-only messages get synthesized
`subject = "{attachments only}"` / `body_text = "{attachments: name1, name2}"`
so they remain searchable and visible (original bytes/filenames are intact
in `messages.attachments` + the blobs tree).

## Search subsystem (Phases 1 + 2 shipped)

Hybrid lexical (tsvector) + vector (pgvector) search over messages and
attachment text. See
[docs/superpowers/specs/2026-05-16-hybrid-search-design.md](docs/superpowers/specs/2026-05-16-hybrid-search-design.md)
for the full design,
[docs/superpowers/plans/2026-05-16-hybrid-search-phase1.md](docs/superpowers/plans/2026-05-16-hybrid-search-phase1.md)
for the Phase 1 plan, and
[docs/superpowers/specs/2026-05-16-hybrid-search-phase2-design.md](docs/superpowers/specs/2026-05-16-hybrid-search-phase2-design.md) /
[docs/superpowers/plans/2026-05-16-hybrid-search-phase2.md](docs/superpowers/plans/2026-05-16-hybrid-search-phase2.md)
for the Phase 2 plan.

- Code lives under `src/localmail/search/` — `chunking.py`, `embeddings.py`,
  `reranker.py`, `query.py`, `searcher.py`, `arms.py`, `page_cache.py`,
  `embed_worker.py`, `extractor.py`, `extract_worker.py`. Public API:
  `localmail.search.create_searcher`.
- All numeric tunables in `LocalmailConfig.search` (`SearchConfig`).
  **No magic numbers elsewhere in search code.**
- Lexical retrieval via PostgreSQL built-in `tsvector` + `ts_rank_cd` with
  `setweight()` — no third-party extension required. Arms 1 and 2 (whole-message
  and chunk-level FTS) use `plainto_tsquery('simple', ...)` for language-neutral
  tokenisation. The docstrings in `arms.py` still use "BM25" as shorthand;
  the actual implementation is `tsvector`/`ts_rank_cd` throughout.
- Vector retrieval via pgvector HNSW + `halfvec(768)`. Default embedder:
  EmbeddingGemma-300M via fastembed (Gemma Terms — runtime download).
- One embed_worker thread per process (account-agnostic; backend-bound).
  Lazily chunks messages it sees without chunks. Failure model mirrors
  `sync.py`:
    - **Per-message SAVEPOINT** around chunking — poison messages land in
      `failed_chunkings` (keyed on `message_id`) and are skipped on
      subsequent sweeps once `retry_count >= embed_worker_max_chunk_retries`.
    - **Per-chunk SAVEPOINT** around the embedding UPDATE — poison chunks
      land in `failed_embeddings` and are skipped likewise.
    - **Both failure-recording paths use a nested SAVEPOINT** so a logging
      failure can't abort the outer transaction.
    - **Batch-level backend errors do NOT mark chunks as failed.** Transient
      errors (network blips, model load failures) just roll back and back
      off; chunks get re-claimed next sweep. Permanently-broken backends
      surface via repeated WARNINGs rather than silently poisoning the
      entire queue.
- Phase 2 (attachment search) — **shipped**, see
  [docs/superpowers/specs/2026-05-16-hybrid-search-phase2-design.md] and
  [docs/superpowers/plans/2026-05-16-hybrid-search-phase2.md].
  Phase 3 (MCP), Phase 4 (--smart), Phase 5 (polish) — separate design + plans.

**Phase 2 notes**:
- `LightweightExtractor` handles 11 formats (PDF, DOCX, XLSX, PPTX, ODT, RTF,
  TXT, Markdown, HTML, CSV, ICS). `DoclingExtractor` is optional, enabled via the
  `[extraction]` uv extra.
- `extract_worker` uses `conn_factory` (not pool) so each sweep gets a fresh
  connection — prevents server-side idle timeouts on long extractions.
- `extract_worker` spawn is gated by `cfg.search.run_extract_worker`.
- There is no `failed_attachment_chunkings` table (intentional Phase 2 scope
  decision); persistent attachment-chunk failures surface as repeated WARNING logs.
- `_extract_xlsx` blob-path workaround: openpyxl detects format by file extension,
  so the worker passes `io.BytesIO(path.read_bytes())` instead of the
  extension-free blob path. No other Office extractor has this issue.

**`bm25_field_boosts` weight normalization**: `arms.py` normalises the raw
boost values by `max(raw)` to satisfy `ts_rank_cd`'s `[0, 1]` weight
requirement. Config values above 1.0 are therefore treated as *relative*
weights, not absolute — e.g. `{"subject": 3.0, "from": 2.0, "body": 1.0,
"to": 0.5}` is equivalent to `{"subject": 1.0, "from": 0.67, "body": 0.33,
"to": 0.17}` after normalization.

**`body_html` in FTS (migration 0006)**: the generated column `fts_v2` on
`messages` includes `body_html` concatenated with `body_text` at weight C.
This deviates slightly from the plan (which had only `body_text`). HTML
markup tokens (tags, attribute names) may dilute ranking slightly for
heavily-marked-up messages; this can be revisited in a later migration if
needed. The current approach is fine for plain-text–heavy archives.

**`_split_statements` in `db.py`**: the migration runner delegates to
`sqlparse.split` so dollar-quoted bodies (`$$ ... $$` / `$tag$ ... $tag$`),
single-quoted string literals, and `--` / `/* */` comments don't trip the
splitter on embedded semicolons. Pure-comment fragments after the final
statement are dropped; comments attached to a real statement are preserved.

**Acceptance eval harness**: `tests/acceptance/run_recall_eval.py` seeds the
synthetic multilingual corpus, runs the embed worker, and reports recall@K +
MRR@K per language. Phase-1 gates: recall@20 >= 80% and MRR@20 >= 0.5 for
de/en/es/ja. Norwegian is reported but not gated. Requires the fastembed model
`google/embeddinggemma-300m` to be in the local fastembed cache (downloaded
on first invocation, ~250 MB).

**RRF sweep harness**: `tests/acceptance/run_rrf_k_sweep.py` (added for #35)
seeds the chosen corpus + drains the workers exactly once, then re-runs the
query suite for each candidate `rrf_k` against the same chunk pool — only
fusion varies between sweeps. Use `--corpus {multilingual,attachment}`,
`--rrf-ks`, `--candidates-per-arm`. The #35 measurement showed that both
synthetic corpora are insensitive to `rrf_k` across [1, 1000] — fusion is
dominated by a single arm — so the default `rrf_k=60` is fine until
production data or an adversarial corpus exposes the bias hypothesised in
#35.

## GUI server (Phase 1 of GUI)

Network-reachable HTTPS API exposing the same functionality as the search
subsystem, plus account/folder/message/attachment read paths and bearer-token
auth. See [docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md)
for the full design.

- Code lives under `src/localmail/api/` (transport-free service library) and
  `src/localmail/serve/` (FastAPI HTTP wrapper).
- The MCP server (planned) will import `localmail.api` directly — no HTTP hop.
- Migration `0014_api_users.sql` adds `api_users` + `api_tokens`. Tokens are
  stored as SHA-256 hashes; raw bearer is only returned at login/refresh.
- Migration `0016_user_accounts.sql` adds the per-user `(user_id, account_id)`
  ACL join table. Every service-layer accessor under `src/localmail/api/`
  takes a required keyword-only `allowed_account_ids: list[int]` so the SQL
  boundary applies `WHERE account_id = ANY(%s)` on every read. Routes
  resolve the list once per request via `localmail.api.acl.allowed_account_ids`.
  See [docs/superpowers/specs/2026-05-18-per-user-account-acl-design.md](docs/superpowers/specs/2026-05-18-per-user-account-acl-design.md).
- The page cache namespaces cursors by `user_id` so a search cursor minted
  by user A and replayed by user B is treated as a cache miss — preventing
  cross-user pool leakage.
- TLS is on by default; `--no-tls` is only accepted with `--bind 127.0.0.1`.
- The HTTP server and the sync daemon never call each other — they share
  Postgres and can run independently.
- **Attachment download policy (#32 phase 1)**: `/v1/attachments/{sha256}`
  always emits `Content-Disposition: attachment` with both the legacy
  ASCII `filename=` and the RFC 5987 `filename*=UTF-8''…` form, so the
  browser is forced into a download (never inline render — the XSS sink
  in stored HTML/SVG blobs). MIME types in `_INLINE_RISKY_MIMES`
  (`text/html`, `application/xhtml+xml`, `image/svg+xml`, `text/xml`,
  `application/xml`) are clamped to `application/octet-stream` on the
  wire as defense in depth (the DB row is untouched). These invariants
  apply to **every** response — full GET, 206 Partial Content, *and*
  416 — so a proxy or client can never be tricked into rendering a
  ranged slice inline.
- **Range support (#54, phase 2 of #32)**: `/v1/attachments/{sha256}`
  advertises `Accept-Ranges: bytes` and honours `Range: bytes=…` per
  RFC 9110 §14.1. Parsing lives in
  [`src/localmail/api/range_requests.py`](src/localmail/api/range_requests.py)
  as a pure module (no IO, no FastAPI) so it's reusable by future
  transports (MCP, etc.). Contract:
    - Single closed range (`bytes=0-9`), open-ended (`bytes=10-`), and
      suffix (`bytes=-10`) → 206 with `Content-Range: bytes start-end/total`.
    - End past EOF is **clamped** to `size - 1` (RFC 9110 §14.1.2).
    - Start past EOF or suffix-of-0 → 416 with `Content-Range: bytes */N`.
    - Unparseable Range headers fall through to 200 full-response (RFC
      permissive branch — servers MAY ignore unsupported syntax).
    - Multi-range (`bytes=0-9,20-29`) also falls through to 200 — we
      don't emit `multipart/byteranges`; single-range covers PDF/video
      seek and connection-resume, which is all the GUI needs.
  Streaming uses `.seek(start)` + bounded chunked `read()` (never slurps
  the whole blob into memory) and still goes through `open_attachment_bytes`,
  so the per-user ACL applies to ranged requests too.
- **Short-read detection (#58)**: both `_stream_full` and `_stream_range`
  in [`src/localmail/serve/routes/attachments.py`](src/localmail/serve/routes/attachments.py)
  count bytes actually yielded and call `_log_truncation()` (WARNING on
  the `localmail.serve` logger, message
  `attachment stream truncated: sha256=… expected=… sent=…`) when the
  on-disk blob runs out before the DB-recorded `attachment_blobs.size_bytes`
  (or, on the 206 path, before the requested slice length). Headers are
  already flushed at that point, so the response is short and the client
  sees a stalled / prematurely-closed connection — the log is the only
  ops signal. Don't try to "patch up" the wire here. If a downstream
  consumer ever needs a pre-stream sanity check, add a `stat()` gate
  before the headers go out; the issue body for #58 explicitly scoped
  that out as not necessary.
- **Conditional GET — ETag / If-None-Match / If-Range (#59)**: the
  attachment route advertises a **strong** ETag of `"<sha256-hex>"` on
  every 200 / 206 / 416 response — content-addressable URLs make the
  ETag canonically strong and immutable, so it can be cached
  indefinitely. Parsing lives in
  [`src/localmail/api/conditional.py`](src/localmail/api/conditional.py)
  as a pure module (no IO, no FastAPI) for the same reason
  `range_requests.py` is — future transports (MCP, etc.) reuse it.
  Comparison rules per RFC 9110:
    - `If-None-Match` (§13.1.2) uses **weak** compare. `*`, exact
      strong, and weak (`W/"…"`) variants of the current SHA all match
      → 304 Not Modified with **no body**, carrying only the `ETag`
      header (no Content-Disposition / Accept-Ranges / Content-Length
      — §15.4.5 representation-metadata rules). Evaluated **before**
      Range, so a 304 never degrades to 206 even when both headers
      are present.
    - `If-Range` (§13.1.5) uses **strong** compare. On match, the
      Range proceeds and a 206 is served as today. On mismatch (weak
      tag, HTTP-date, garbage, or simply the wrong SHA) the Range is
      **ignored** and a full 200 is served — never stitch a resumed
      download onto a stale prefix.
    - `If-Range` without `Range` is a no-op (RFC 9110 forbids it; we
      tolerate it gracefully).
  Note that the ETag is `"<sha>"` quoted — `etag_for_sha256` returns
  exactly that; don't double-quote. The pure helpers are
  intentionally generic over `etag` so non-SHA streaming endpoints
  could reuse them.
- **ID typing (#33)**: every entity ID is a **string on the wire** in
  both directions — response bodies emit `str(id)` and path/query
  parameters accept digit-strings only. `localmail.api.ids.parse_int_id`
  is the single boundary cast: route handlers call it on `account_id` /
  `message_id` / `since` cursor and surface a uniform `400
  /problems/validation-failed` on non-digit input (including `+`/`-`,
  whitespace, decimals, hex prefixes, Unicode digits). The api/ layer
  still takes `int`, so the cast happens exactly once per request. When
  adding a new ID-bearing endpoint or MCP tool, declare the parameter
  as `str` and call `parse_int_id(...)`; never accept `int` directly
  from the wire, and never bypass the helper.

## Conventions

- **No comments unless the WHY is non-obvious.** Don't restate the SQL or the
  Python.
- **Don't write `.eml` fixtures to disk** — `tests/_eml.py` builds messages
  programmatically with `email.message.EmailMessage`. Same goes for any future
  test fixture: generate, don't check in.
- **DB tests** TRUNCATE before each test (see the `db_conn` / `pool` fixtures).
  Tests must work against the live test DB; never `DROP TABLE`.
- **No `cur.fetchone()[0]` without `assert row is not None` first** — mypy is
  enabled (`[tool.mypy]` in `pyproject.toml`) and will flag it.
- New SQL goes in a new numbered migration file. **Never edit a migration
  that has been applied anywhere** — add the next-numbered file instead.
  Latest is `0018_messages_date_received_internaldate.sql`; next would be `0019_*.sql`.

## Testing notes

- `LOCALMAIL_TEST_DSN` defaults to the **`localmail_test`** database, not the
  live `localmail` one. This is intentional and important — running pytest
  must not touch live archives.
- The `memory_keyring` fixture (autouse) intercepts every `keyring` call so
  real Keychain entries aren't written/read during tests.
- `tests/_fake_imap.py::FakeIMAPClient` is the only place to extend when sync
  or daemon code needs new IMAP verbs.

## Known gaps / non-goals (deliberate)

- No write path to IMAP (no sending, no flag changes, no deletion).
- No web UI / API — downstream agents read directly.
- No multi-host clustering — single-host daemon.
- Gmail "In production" OAuth verification is not pursued; the project stays in
  "Testing" mode with the user as an explicit Test User.
