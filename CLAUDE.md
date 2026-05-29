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
uv run localmail estimate-upgrade [--format text|json]   # pre-flight size/duration for lock-heavy migrations
# see docs/operations/upgrade-runbook.md
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
migrations/         # 0001_init.sql … 0022_api_users_sessions_invalidated_at.sql (0020_accounts_canonical.sql also applied)
tests/
  acceptance/       # standalone eval harnesses (run_recall_eval.py,
                    # run_attachment_eval.py, run_rrf_k_sweep.py,
                    # run_browse_explain.py)
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
`user_accounts`, `schema_migrations`. Migration `0020_accounts_canonical.sql`
extended `accounts` with `folder_allow`, `folder_deny`, `folder_deny_flags`,
`sync_enabled`, `updated_at`, lifted the `NOT NULL` constraint from
`imap_host`/`imap_port`, widened `auth_method` to include `'archive'`, and
added the `accounts_live_requires_host` check constraint (live accounts must
have a host). Dedup model:

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
backed by the expression index `messages_recent_idx`. Used by the
`/v1/changes` initial-fetch branch, the `Searcher` empty-query fallback
(`_list_recent_messages`), the new keyset browse path
(`api.list_messages` → `/v1/messages`), and the `sort=date` lexical
keyset path (`Searcher._lexical_date_search`).

**Planner choice under the per-user ACL filter (#72, resolved)**:
`messages_recent_idx` does *not* include `account_id`, but the
planner uses it anyway as a date-ordered walk and applies the
`account_id = ANY(...)` predicate as a per-tuple filter. The
acceptance harness in [tests/acceptance/run_browse_explain.py](tests/acceptance/run_browse_explain.py)
proves this across 200,000-row synthetic archives in balanced /
skewed / tail multi-account distributions: every probe picks
`Index Scan using messages_recent_idx`, never a bitmap heap scan
or full sort. No covering index keyed on `account_id` is needed
(or warranted — it would duplicate the existing
`messages_acct_date_idx` without solving anything the LIMIT
short-circuit doesn't already solve). The index-eligibility
regression is pinned by `tests/test_api_browse_plan.py` —
specifically that the COALESCE expression, the `DESC NULLS LAST`,
and the secondary `id DESC` are all load-bearing for the plan,
and that the index alone can serve the query when competing
indexes are temporarily hidden.

**Mid-keyset perf (#75, resolved)**: deep-keyset pagination
(`cursor.ts` not None) used to walk ~`total_rows / 2` tuples per
51-row page because the cursor predicate (`expr < X OR (expr = X
AND id < Y) OR COALESCE IS NULL`) was treated as a post-walk
`Filter:` rather than an `Index Cond:`. Two interacting causes:
the `OR COALESCE IS NULL` disjunct admitted NULL-tail rows but
prevented any range bound; even after removing it, the OR-form
keyset (`expr < X OR (expr = X AND id < Y)`) still degraded to
a Filter at production scale (Postgres refuses to decompose a
mixed-column OR into an index range bound when an Index Scan
alternative is on the table).

The shipped fix uses SQL **row comparison** —
`ROW(COALESCE(internal_date, date_sent), m.id) < ROW(%s, %s)` —
which Postgres composes as a single `Index Cond` on
`messages_recent_idx`. The scan starts AT the cursor and only
emits matching rows. NULL-tail rows are reached via a separate
"top-up" query in `list_messages` when the dated portion runs
short of `limit + 1`; the response cursor transitions to the
NULL-tail flavour (`ts=None`) naturally via `page_rows[-1]`.

200k-row, ACL=1 heavy, skewed distribution, mid-keyset 51-row
LIMIT: **100,014 → 13 rows removed by filter; 28.3ms → 0.072ms
execution; ~500k → 424 buffer hits**. The residual filter rows
are bounded by the per-tuple ACL cost (~`page_size /
acl_fraction`), not by table size. Tracked by
`tests/test_api_browse_plan.py::test_dated_cursor_predicate_composes_index_range_bound`
(unit-scale eligibility) and
`tests/acceptance/run_browse_explain.py` (operational
`--predicate-form {current,pre75}` before/after). Do NOT
rewrite the predicate as the OR-form even though it's
semantically equivalent — the planner does not optimize it.

**Canonical browse SQL emitter (#77, simplified by #85)**:
`BROWSE_ROW_SQL_TEMPLATE`, `compose_browse_sql(where=…)`, and
`build_where(account_ids=…, folder_ids=…, cursor=…,
null_tail_only=…)` in
[src/localmail/api/browse.py](src/localmail/api/browse.py) are
the only authoritative SQL emitter for the browse path. Both
the unit-scale eligibility tests
(`tests/test_api_browse_plan.py`) and the EXPLAIN harness
(`tests/acceptance/run_browse_explain.py`) compose the
production SQL via these primitives — there is no duplicate
inline SQL to drift against. Any refactor of the SELECT /
FROM / ORDER BY shape or of the WHERE-clause emitter
automatically lands in the tests + harness. The `pre75`
predicate variant in the harness is the one deliberate
exception: it reuses `BROWSE_ROW_SQL_TEMPLATE` for the
SELECT / FROM / ORDER BY shape but substitutes a local
buggy `_PRE75_BUGGY_WHERE` so the operator can reproduce
the pre-fix planner choice.

**Folder-filter shape (#78, simplified by #85 — EXISTS semi-join)**:
the `folder_ids` branch of `list_messages` adds
`AND EXISTS (SELECT 1 FROM message_labels ml WHERE
ml.message_id = m.id AND ml.mailbox_id = ANY(%s))` inside
the WHERE clause; there is **no** `JOIN message_labels` in
the FROM clause and **no** `SELECT DISTINCT`. EXISTS short-
circuits the labels scan on the first matching row per outer
message, so there is no row multiplication and no DISTINCT
is required. Pre-#85 the production SQL used `SELECT
DISTINCT … JOIN message_labels …`, which forced a post-join
Sort+Unique pass over every projected column on top of the
Nested Loop; the EXISTS rewrite turns that 3-node chain
(`Nested Loop + Incremental Sort + Unique`) into a single
`Nested Loop Semi Join`. The #85 benchmark at 200k rows ×
broad folder showed ~45-50% fewer buffer hits per page
across every folder-filter probe; the operationally
significant signal is the buffer-hit reduction, not the
sub-ms execution time delta (synthetic data fits in cache).
The planner's choice for the *messages* side of the
semi-join is still selectivity-dependent — at narrow
selectivities (~5% labelled) it can correctly start from
`message_labels`; at broad selectivities (~50%) it prefers
the date-ordered `messages_recent_idx` walk. At production
scale every folder-filter probe picks `Index Scan using
messages_recent_idx`. The acceptance harness exercises this
via `run_browse_explain.py --folder-filter`, which seeds two
mailboxes per account (`selective` ~5%, `broad` ~50%) and
appends four folder-filter probes: ACL=1+selective,
ACL=1+broad, ACL=1+broad mid-keyset, ACL=all+broad-across-
accounts. The SQL-shape eligibility regression is pinned by
`tests/test_api_browse_plan.py` —
`test_messages_recent_idx_is_eligible_for_{narrow,broad,multi}_folder_filter`
prove that with every competing `messages` index hidden,
`Index Scan using messages_recent_idx` still serves the
messages side under the semi-join. Those tests do NOT
forbid Sort nodes — at fixture scale the planner correctly
inverts the semi-join (starts from `message_labels`, looks
up messages by PK via `messages_recent_idx`, then Sorts to
restore the ORDER BY); the DISTINCT-regression signature
(`Unique` node + Sort over every projected column) only
surfaces at scales where the date-ordered walk is preferred,
which the acceptance harness covers.

**The wire `date` field MUST match this sort key.** Every paginated
list endpoint (`/v1/messages`, `/v1/search`, `/v1/changes`) emits
`date = COALESCE(internal_date, date_sent)` — never raw `date_sent`.
Emitting `date_sent` while the SQL sorts by COALESCE makes the
displayed order look broken on any row whose two dates disagree
(forwarded mail, mailing lists, sender clock skew, mid-rollout
backfill). Tests in `test_serve_browse_route.py`,
`test_serve_search_route.py`, `test_serve_changes_route.py`
enforce this invariant — keep them green when touching wire
serialisation.

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
- **Login rate-limiting (Postgres-backed, #7)**: migration
  `0019_api_login_attempts.sql` adds an append-only audit table read by
  three sliding-window caps — global, per-IP, per-user. Every login
  attempt (success + failure) is one INSERT; the check is a single SELECT
  with three `FILTER (...)` aggregates. Caps + windows + retention live
  in `LocalmailConfig.auth` so there are no module-level magic numbers in
  `api/auth.py`. The in-memory dicts that preceded this design were
  per-process and lost the security promise the moment `uvicorn
  --workers N` came into scope; the DB-backed table keeps the limits
  consistent across workers and across `serve` restarts. Cleanup is
  best-effort, gated by a Postgres advisory lock
  (`_SWEEP_ADVISORY_LOCK_KEY`) so concurrent workers don't pile up
  DELETEs. **Reverse-proxy support**: `auth.trusted_proxies` (CIDR list)
  + `auth.trusted_proxies_max_hops` enable right-to-left peeling of
  `X-Forwarded-For` for the per-IP cap. Empty default = unchanged
  behaviour (the socket peer is used). The same CIDR list governs both
  admission (is the immediate peer a trusted proxy?) and peeling
  (which XFF entries to skip). Design + threat model in
  [docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md](docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md).
  Do NOT also set `uvicorn --forwarded-allow-ips`; it rewrites
  `request.client.host` before our admission check and collapses it.
- **Admin session revocation (#113)**: migration
  `0022_api_users_sessions_invalidated_at.sql` adds a nullable
  `sessions_invalidated_at TIMESTAMPTZ` column on `api_users`. The
  admin-cookie dependency (`localmail.serve.admin.dependencies.require_admin_session`)
  passes the session token's `issued_at` into `get_admin_user`; the
  service does `to_timestamp(issued_at) < sessions_invalidated_at`
  in the same SELECT and raises `SessionInvalidated` when the token
  predates the revocation moment — translated to a 303 redirect to
  `/admin/login`. NULL means "never revoked" and is the default.
  Operators bump the column shell-side via
  `localmail revoke-admin-sessions USERNAME`; admin privileges are
  untouched (use `revoke-admin` for that). The check is opt-in:
  callers that don't pass `issued_at` (CLI lookups, smoke paths)
  skip the comparison entirely so they keep working on a
  revoked user.
- **DB-canonical accounts + admin CRUD (Sub-plan 2A)**: migration
  `0020_accounts_canonical.sql` makes `accounts` the write-authoritative
  store for IMAP configuration — adding `folder_allow`, `folder_deny`,
  `folder_deny_flags` (RFC 6154 flag-based denial), `sync_enabled`,
  `updated_at`; lifting NOT NULL from `imap_host`/`imap_port`; extending
  `auth_method` to include `'archive'`; and adding the
  `accounts_live_requires_host` check constraint so live accounts always
  carry a host. The v1 daemon does not yet honour `sync_enabled` (deferred
  to Sub-plan 2A.2 along with TOML→DB seed and CLI rewiring). The service
  layer in
  [`src/localmail/api/admin/accounts.py`](src/localmail/api/admin/accounts.py)
  exposes `list_accounts`, `get_account`, `create_account`,
  `update_account`, `delete_account` (cascade-or-refuse: refuses when
  messages exist unless `force=True`), `store_password`,
  `clear_secret`, and `probe_connection` (renamed from `test_connection`
  to avoid pytest auto-collection). The web OAuth flow for Gmail accounts
  lives in
  [`src/localmail/api/admin/oauth.py`](src/localmail/api/admin/oauth.py)
  — `start_oauth` returns a Google consent URL and writes a stateless
  HMAC-signed state token via
  [`src/localmail/api/admin/oauth_state.py`](src/localmail/api/admin/oauth_state.py)
  (`encode_state`/`decode_state`: JSON payload + `base64url(hmac_sha256(key,
  payload))`); `complete_oauth` verifies the state, exchanges the code,
  and persists the refresh token — closes #114 (`[serve].state_signing_key`
  now has a real consumer). HTTP routes for CRUD + password + test-connection
  live under `/v1/admin/accounts` (the test-connection URL keeps the
  `test-connection` name for API consistency even though the Python function
  is `probe_connection`); OAuth routes are `POST
  /v1/admin/accounts/{id}/oauth/start` and `GET /admin/oauth/callback`. The
  callback reads `state`/`code` via `get_unscrubbed_query_params(request)`
  because `ScrubSensitiveQueryParamsMiddleware` would otherwise redact them.
  Cookie `Path` is `"/"` — required so the admin session cookie reaches
  `/v1/admin/*` routes; SameSite=Lax + per-route CSRF tokens
  (`X-CSRF-Token` header) remain the primary CSRF defences. The JSON-router
  CSRF token is bound to `(user_id, "<METHOD>:<action-url>")` —
  `check_csrf` derives the method from `request.method` via
  `serve/admin/csrf.py::csrf_action`, so a token minted for `PATCH` on a
  shared URL can't be replayed against `DELETE` (#122). No `/v1/*` machine
  endpoint reads cookies (machine clients use `Authorization: Bearer …`),
  so the broader cookie scope adds no smuggling surface — pinned by
  [tests/test_session_cookie_scope.py](tests/test_session_cookie_scope.py),
  which walks the FastAPI dependant tree and fails if any non-`/v1/admin/*`
  route under `/v1/` reads the session cookie or depends on
  `require_admin_session` (#121). The OAuth flow's
  `gmail_oauth.client_secrets_file` is threaded in from
  `app.state.gmail_client_secrets_file` (set by `create_app`'s
  `gmail_client_secrets_file=`) — the service layer never calls
  `load_config()` per request (#120). When that path is unset,
  `oauth.py::_build_flow` raises `OAuthNotConfigured` (a
  `RuntimeError` subclass), which `oauth_start` maps to a clean
  **503** "Gmail OAuth is not configured" rather than letting a bare
  `RuntimeError` escape as a 500 (#126); the callback's broad
  `except Exception` already catches it as a failed-redirect.
  Account-row reads use psycopg
  `class_row` (name-based column→field mapping, not positional unpack), and
  `AccountInUse` subclasses `ValueError` like its sibling
  `AccountFieldError` (#119, #123).
- **TOML→DB seed (Sub-plan 2A.2 slice 1, shipped):** `init-db` now merges
  `config.toml` `[[accounts]]` into the `accounts` table after migrations
  apply — idempotent, keyed by `name`, **DB-canonical** (existing rows are
  never overwritten; a drifted TOML value logs a WARNING naming the fields
  and is otherwise ignored). Implemented as a pure planner
  (`account_seed.plan_account_seed`) + thin IO wrapper
  (`account_seed.seed_accounts`, inserting via `create_account` to reuse
  validation, reading existing rows via the new public
  `api.admin.accounts.list_accounts_full`); `init-db` echoes
  `seeded accounts: inserted=N skipped=M drifted=K` and maps a malformed
  block's `AccountFieldError` to a clean non-zero `ClickException` (whole
  seed runs in one uncommitted transaction, so a failure leaves no partial
  rows). Still deferred to later 2A.2 slices: rewiring CLI `add-account` /
  `oauth-login` / `remove-account` to the DB, switching the daemon's account
  source to the DB, and honouring `sync_enabled`. Note `sync.py:upsert_account`
  still overwrites `email/host/port/auth_method/oauth_provider` from config
  on first sync, so the DB is not yet *fully* canonical against the running
  daemon until the daemon-source slice lands.
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
- **304 short-circuit skips file-open + filename lookup (#62)**: the
  `stream_blob` route uses a cheap two-step probe before deciding to
  serve a body. First `get_attachment_blob_info` (DB-only: ACL +
  `attachment_blobs` row → `(mime, size, path)`, no `Path.exists()`,
  no JSONB filename scan). Then `if_none_match_satisfies` → if it
  fires, return 304 and never call `open_attachment_bytes` /
  `get_attachment_filename`. Only the body-carrying path pays for the
  file open and the JSONB scan that picks the per-message original
  filename. **The probe runs the same ACL check as
  `open_attachment_bytes`**, so a caller without a grant still sees
  404 — never 304 — even when their `If-None-Match` would otherwise
  satisfy. Tested by
  [`test_serve_attachments_conditional.py::test_304_does_not_call_open_attachment_bytes_or_filename`](tests/test_serve_attachments_conditional.py)
  (spy-on-imports asserts zero invocations) and
  [`test_304_acl_denied_returns_404_not_304`](tests/test_serve_attachments_conditional.py)
  (no grant → 404 even with matching If-None-Match). When adding any
  new conditional-GET endpoint, follow the same probe → conditional
  → expensive-IO ordering; never put the expensive call before the
  precondition check.
- **200/206 body path reuses the probe's row (#64, #67)**: the route
  uses the ACL-cleared `(mime, size, path)` tuple from
  `get_attachment_blob_info` directly. The file open goes through the
  module-private `_open_blob_file_at(path, sha256_hex)` helper in
  `api/attachments.py`, which does only `Path.exists()` + `Path.open('rb')`
  and has no `conn` parameter at all — so the ACL check cannot be
  forgotten "by accident". End-to-end on a 200 there is exactly one
  `_caller_can_read_blob` call and one `attachment_blobs` SELECT
  (the probe's), enforced by
  [`test_200_runs_exactly_one_acl_check`](tests/test_serve_attachments_conditional.py).
  `_open_blob_file_at` raises `NotFound` if the file is missing so a
  blob deleted between probe and open surfaces cleanly rather than
  as a mid-stream `FileNotFoundError`. `get_attachment_filename`
  remains a separate JSONB scan — same predicate shape, different
  query — and is out of scope for the #64 ACL-collapse acceptance.
  All three blob-row accessors (`get_attachment_metadata`,
  `get_attachment_blob_info`, `open_attachment_bytes`) share a single
  private `_lookup_blob_row` helper (#65). `open_attachment_bytes`
  itself is safe-by-default — it always runs the ACL EXISTS predicate
  and has no `prefetched=` kwarg (#67 removed the prior footgun).
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
- **Browse & search pagination (PR #70)**:
  - `GET /v1/messages` is the canonical keyset browse endpoint, ordered
    `COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC` with
    an opaque `next_cursor` (URL-safe base64; `localmail.api.browse_cursor`).
    The GUI's initial mail-list load goes here, not `/v1/changes`.
  - `GET /v1/search` returns one of **two cursor flavours**, distinguished
    by prefix on the wire:
      - `"<token>:<page>"` — pool cursor for `sort=rank` (and for
        `sort=date` with an empty query). Driven by `Searcher.continue_page`
        / `Searcher.grow_pool`. The route doubles `candidates_per_arm` up to
        `search.candidates_per_arm_max` (default 800) when the page would
        advance past the cached pool; once the ceiling is hit `next_cursor`
        flips to `null`.
      - `"K|<base64>"` — keyset cursor for `sort=date` with a non-empty
        query, served by `Searcher._lexical_date_search`. Same FTS column
        as retrieval Arm 1 (`fts_v2 @@ plainto_tsquery('simple', q)`), so
        recall is identical to the lexical case. No pool cap; unbounded
        scroll. Route dispatches on the `K|` prefix.
  - **Page-cache miss surfaces as HTTP 409 `/problems/search-cursor-expired`,
    never a 500.** TTL eviction, LRU eviction, and cross-user replay all
    take this path. The GUI re-runs the query without a cursor on 409 and
    appends past rows it already holds — keep this transparent recovery
    working when touching `serve/routes/search.py` or `api/search.py`.
  - **`reranker_enabled` defaults to `False`.** The cross-encoder is
    O(pool size) and the cursor's `grow_pool` doubles the pool on each
    miss (50 → 100 → … → 800). On CPU that overruns request timeouts.
    Operators on GPU opt back in via `[search] reranker_enabled = true`
    in `config.toml`. Don't quietly flip this default; the rerank fanout
    cost compounds with the pagination work.
  - **Known follow-ups (filed)**: #72 (`EXPLAIN ANALYZE` under the
    per-user ACL filter on `messages_recent_idx`). `grow_pool` on the
    `sort=rank` path can still surface duplicates when the cache is
    exhausted past pool 100 — covered by `sort=date` for the "show me
    everything" intent.
  - **Searcher public boundaries (#71)**: the api/ layer (and any
    future MCP layer) uses `searcher.get_pool_metadata(token, *,
    user_id)` and `searcher.config` — never reach into
    `searcher._cache` or `searcher._cfg`. The accessor's `user_id`
    scoping mirrors `continue_page` / `grow_pool` exactly. Tests in
    `tests/test_searcher_pool_metadata.py` enforce.

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
  Latest is `0022_api_users_sessions_invalidated_at.sql`; next would be
  `0023_*.sql`. (`0020_accounts_canonical.sql` has now shipped — the gap
  is filled.)

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
