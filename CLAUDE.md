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
uv run localmail enable-account N    # resume syncing account N (sync_enabled = TRUE)
uv run localmail disable-account N   # pause syncing account N (sync_enabled = FALSE)
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
    rewriter.py     # Phase 4 --smart: build_rewrite_prompt/parse_rewrite_response/apply_rewrite (pure) + PEP562 back-compat re-exports
    rewriter_backends.py # _HttpJsonRewriter base + Ollama/OpenAI/Anthropic backends + build_rewriter() factory
    searcher.py     # Searcher orchestrator, rrf_fuse(), make_snippet(), SearchResult
migrations/         # 0001_init.sql … 0031_oauth_resource_indicator.sql (0023_daemon_heartbeats.sql also applied)
tests/
  acceptance/       # standalone eval harnesses (run_recall_eval.py,
                    # run_attachment_eval.py, run_rrf_k_sweep.py,
                    # run_browse_explain.py, run_chunk_insert_bench.py)
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
`failed_messages`, `failed_extractions`, `transient_extractions`,
`api_users`, `api_tokens`, `channel_subscriptions`,
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

  **Startup backoff (#133)**: `Daemon.__init__` does DB IO during
  construction — `_load_syncable_accounts` (a one-shot `psycopg.connect`,
  before the pool opens, since pool sizing needs the account count) then
  `open_pool`. The **synchronous** `_load_syncable_accounts` touch goes
  through `retry.retry_with_backoff` so a briefly unreachable Postgres at
  launch (DB still coming up under systemd, transient blip) makes the daemon
  *wait* — bounded exponential backoff between
  `daemon.startup_backoff_initial_s` (default 1.0) and
  `daemon.startup_backoff_max_s` (default 60.0) — rather than crashing on
  construction. `open_pool` is **not** wrapped: it opens with `wait=False`
  (returns immediately, fills lazily on background threads) and so never
  raises synchronously on an unreachable DB — wrapping it would catch only
  config errors, which aren't transient. By the time `_load_syncable_accounts`
  returns, Postgres has answered; a blip in the window before a worker first
  acquires a connection is absorbed by the IDLE/poll loops' own 1s→60s
  backoff. The shared `retry.next_backoff` (pure: `min(current*factor, max)`)
  plus `retry_with_backoff` (respects the stop event; first attempt is
  immediate; a stop signal during a wait raises `RetryAborted`) live in
  [src/localmail/retry.py](src/localmail/retry.py). Signal handlers install in
  `run_forever` *after* construction, so during a startup-backoff wait
  SIGTERM/SIGINT fall to the default handler (process exits) — the
  `RetryAborted` escape is for an injected `stop_event` (tests, future daemon
  control), not the systemd path, where the supervisor owns kill semantics.

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
  `reranker.py`, `query.py`, `rewriter.py`, `searcher.py`, `arms.py`,
  `page_cache.py`, `embed_worker.py`, `extractor.py`, `extract_worker.py`.
  Public API: `localmail.search.create_searcher`.
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
  Phase 5 (polish) — separate design + plans.

**Phase 4 (`--smart` query rewriter) — shipped**, see
[docs/superpowers/specs/2026-06-07-smart-query-rewriter-design.md](docs/superpowers/specs/2026-06-07-smart-query-rewriter-design.md)
and [docs/superpowers/plans/2026-06-07-smart-query-rewriter.md](docs/superpowers/plans/2026-06-07-smart-query-rewriter.md).
Opt-in (`search.rewriter_enabled_by_default` + the per-call `--smart`/`smart=`
flag). [search/rewriter.py](src/localmail/search/rewriter.py) is pure helpers
(`build_rewrite_prompt`, `parse_rewrite_response`, `apply_rewrite`); the IO
backends live in [search/rewriter_backends.py](src/localmail/search/rewriter_backends.py)
— a template-method base `_HttpJsonRewriter` (does prompt-build + parse;
subclasses implement only `_request`) plus three `httpx`-only backends selected
by `search.rewriter_backend` (`ollama` default | `openai` | `anthropic`) via the
`build_rewriter(cfg)` factory. `OllamaLLMRewriter` → Ollama `/api/generate`
(`format`-constrained JSON); `OpenAICompatRewriter` → any OpenAI-compatible
`/chat/completions` (`response_format` json_object); `AnthropicRewriter` →
Anthropic `/v1/messages` (assistant `"{"` prefill forces JSON, no tool-use). All
use `temperature=0`. The cloud backends read their API key at construction from
the env var named by `rewriter_openai_api_key_env` / `rewriter_anthropic_api_key_env`
(never config/DB); a missing key raises `MissingApiKey`, which `create_searcher`'s
guard turns into graceful "no `--smart`". `rewriter.py` keeps the old deep import
path working via a PEP 562 `__getattr__`. No new uv extra (`httpx` is already a dep). The rewriter produces `rewritten_text` (vector arm +
reranker), `expansion_terms` (OR-ed into the lexical arms — see below), and
`extracted_filters` (NL → structured). **`apply_rewrite` merge precedence:
explicit operators win** — the LLM fills only the scalar filter slots
(`after`/`before`/`from`/`to`/`subject`/`has_attachment`) the user left `None`;
it never sets account/folder/lang. **Failure policy lives in the Searcher, not
the rewriter**: the backends raise typed exceptions
(`httpx.HTTPError` subclasses, `RewriteParseError` — incl. a 200-with-missing-
`response`-key); `Searcher.search` catches `(httpx.HTTPError, RewriteParseError)`,
keeps the un-rewritten query, logs `smart rewrite skipped: …`, and surfaces it
on **`SearchPage.rewrite_skipped`** (the CLI prints a `note:`). Relative dates
are resolved LLM-side via an injected `today` (deterministic prompt; testable).
Expansion terms OR into the lexical arms through
`arms.build_lexical_tsquery(free_text, expansion_terms)` →
`plainto_tsquery('simple', %s) [ || … ]`; **with no expansion terms it returns
the bare single-tsquery form byte-for-byte**, so the non-smart path is
unchanged. The multi-term fragment is **parenthesised** because `@@` binds
tighter than `||` in Postgres. `rewriter_max_expansion_terms` (default 8) caps
the OR fan-out. No new migration, **no new uv extra** (`httpx` is already a dep;
Ollama is an external HTTP service). `continue_page`/`grow_pool` reuse the
cached enriched `parsed` and do not re-rewrite (`rewrite_skipped` is a page-1
signal).

**`--smart` over the wire (HTTP + MCP):** the rewriter is also exposed on the
network read surfaces — `POST /v1/search` accepts a `smart` body field and the
MCP `search` tool a `smart` param; both responses carry `rewrite_skipped`
(always present, default `false`). `api.search.run_search` gates it via the
public **`Searcher.smart_available`** property (`self._rewriter is not None`) —
never reaching into `searcher._rewriter` (#71). It computes `effective_smart =
smart and searcher.smart_available` so the Searcher's "no rewriter configured"
`RuntimeError` is never triggered: when `smart` is requested but unavailable,
the un-rewritten query runs and `rewrite_skipped` is `true` (**graceful
degradation** — unlike the CLI, which hard-errors, being interactive). `smart`
applies on the page-1 branch only (`cursor is None`); continuation/keyset pages
report `rewrite_skipped=false`. See
[docs/superpowers/specs/2026-06-08-smart-over-mcp-http-design.md](docs/superpowers/specs/2026-06-08-smart-over-mcp-http-design.md).

**Structured rewrite outcome (#176, #175):** every search response also carries
`rewrite_status` — a 5-value enum (`applied` / `unavailable` / `failed` /
`not_attempted` / `not_requested`) — and an optional curated `rewrite_note`
(actionable detail, e.g. `rewriter model '…' is not available; pull it with:
ollama pull …`). `rewrite_skipped` is **kept but now derived**
(`rewrite_skipped_for_status(status) == status in {unavailable, failed}`). The
pure module [search/rewrite_status.py](src/localmail/search/rewrite_status.py)
holds the constants, the `classify_rewrite_failure(exc, *, model)` classifier
(curated messages only — no raw exception text on the wire; model name is the
sole interpolated value), and `rewrite_skipped_for_status`. `Searcher.search`
classifies its own page-1 outcome onto `SearchPage.rewrite_status` /
`.rewrite_note` (the `rewrite_skipped` *field* is gone from `SearchPage`);
`api.search.run_search` owns the layer-specific statuses — `unavailable` (smart
requested, no rewriter), `not_attempted` (continuation page — **the #176 fix**
for the silent-no-op), and `not_requested` (smart off, or the empty-ACL
short-circuit). The empty-ACL short-circuit also reports `total_estimate: None`
(uniform with the normal path — **#175**; never `0`). See
[docs/superpowers/specs/2026-06-08-rewrite-outcome-status-design.md](docs/superpowers/specs/2026-06-08-rewrite-outcome-status-design.md).
Every response also carries a machine-readable **`rewrite_note_code`** (1:1 with
the curated note, `null` when the note is `null`): `missing_model` / `unreachable`
/ `unparseable` (the three `failed` causes), `not_configured` (`unavailable`),
`continuation_page` (`not_attempted`). The **code is canonical** —
`rewrite_status.classify_rewrite_failure(exc)` returns the code (no `model` arg)
and the pure `note_for_code(code, *, model=None)` renders the human note from it,
so the two cannot drift. See
[docs/superpowers/specs/2026-06-15-rewrite-note-code-design.md](docs/superpowers/specs/2026-06-15-rewrite-note-code-design.md).

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
- **Transient classification of third-party docling failures (#47)**:
  `extract_worker._is_transient` recognises only the narrow builtin
  `_TRANSIENT_EXC_TYPES` (`ConnectionError`/`TimeoutError`/`MemoryError`) plus
  `TransientExtractorError` — broadening it (e.g. `OSError`) would mis-classify
  permanent `ENOENT`/`EACCES`. docling's network errors are *third-party*
  classes (`requests`/`httpx`/`urllib3`/`huggingface_hub`/`aiohttp`) outside
  that hierarchy, so `DoclingExtractor.extract` opts them in **at the wrapper**:
  a `convert()` failure whose cause/context chain contains a package in
  `extractor._TRANSIENT_THIRD_PARTY_MODULES` is re-raised as
  `TransientExtractorError` (retried next sweep, not poison-pilled). The
  chain walk is the shared pure `extractor.iter_exc_chain` generator, reused
  by both `_is_transient` and `_exc_chain_has_transient_module`. To add a
  newly-observed transient package, extend the frozenset — never widen the
  builtin `_TRANSIENT_EXC_TYPES`. **Transient retry cap (#153, resolved)**: the
  transient path deliberately never touches `retry_count` (reserved for
  poison-pill semantics), so a *permanently* failing third-party network error
  (`huggingface_hub` 401/403 from a misconfigured token, 404 for a removed
  model) used to re-attempt every sweep forever. The fix adds a **separate**
  counter table `transient_extractions` (migration `0025`, keyed on `sha256`,
  independent of `failed_extractions`): `extract_worker` bumps
  `transient_count` on each transient classification via
  `_record_transient_safely` (nested SAVEPOINT, like `_record_failure_safely`),
  the `_claim_batch` query excludes a blob once `transient_count >=
  cfg.extract_worker_max_transient_retries` (default 5 — larger than the
  poison-pill cap of 3 because transients are often genuinely recoverable, but
  now bounded), and a successful extraction calls `_clear_transient` so the cap
  measures **consecutive** failures only (the claim returns the prior
  `transient_count` as a 5th column so the reset DELETE is skipped on the common
  no-history path). At the cap the worker logs one distinct *"giving up"*
  WARNING instead of repeating the per-sweep retry line. Recovery: `localmail
  retry-failed-extractions` now clears **both** `failed_extractions` and
  `transient_extractions` rows (per-blob with `--sha256`, else all). The pure
  boundary `transient_budget_exhausted(count, cap)` (`count >= cap`) matches the
  SQL `transient_count < cap` filter.

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

**Chunk-insert benchmark (#5, closed not-fixed)**:
`tests/acceptance/run_chunk_insert_bench.py` seeds N multi-chunk messages and
times the chunking loop's per-chunk `cur.execute` (production) against a batched
`cur.executemany` candidate, both inside the same per-message SAVEPOINT. #5
hypothesised that row-by-row chunk INSERTs were a backfill bottleneck. The
measurement (localhost Postgres, 1500 msgs × ~12 chunks) showed the loop is
**tokenization-bound** — ~880 chunks/s regardless of INSERT strategy, because
`chunk_message` spends its time in tiktoken `encode`, not INSERT round-trips. On
localhost `executemany` is ~4% *slower* (per-call batching overhead with no
round-trip latency to amortise). localmail is **single-host**, so Postgres is
always local — the remote-DB scenario where `executemany` would win never
applies. The production loop **stays row-by-row**; #5 is closed on this evidence.
Per-message poison isolation at the INSERT layer is pinned by
`test_embed_worker.py::test_insert_failure_isolates_poison_message_per_savepoint`
(NUL-byte chunk text → INSERT rejected → only that message rolls back).

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
  rows). **Sub-plan 2A.2b shipped (DB-canonical daemon):** the daemon now
  reads its account set from the `accounts` table — `Daemon.__init__`
  enumerates live, `sync_enabled` accounts via
  `api.admin.accounts.list_syncable_accounts` and maps each row to an
  `AccountConfig` through the pure `daemon_accounts.account_config_from_row`
  adapter (archive + `sync_enabled = FALSE` rows spawn no threads — 2A.2c
  folded in). The DB `account_id` is carried on `WorkerContext`, so the
  IDLE/poll workers use `ctx.account_id` and no longer call `upsert_account`.
  Per-account `poll_seconds` TOML overrides are no longer honoured
  by the daemon (no DB column); the daemon-wide `cfg.daemon.poll_seconds`
  applies to every account. **The account set is read once at `Daemon.__init__`**
  (a one-shot `psycopg.connect`, before the pool opens, since pool sizing
  depends on the count) — admin-UI/CLI account changes take effect on the next
  daemon restart, not live; hot reload is deferred to daemon control (2B).
  **Sub-plan 2A.2d shipped (DB-canonical CLI):** `list-accounts`,
  `add-account`, `oauth-login`, `remove-account`, and the one-shot `localmail
  sync` now read/write the `accounts` table via `api.admin.accounts` instead of
  `cfg.accounts`. `sync.py:upsert_account` is **deleted** (no callers remain);
  `sync.sync_account` now takes an explicit `account_id: int` resolved by the
  caller (it never creates the account row). `add-account` / `oauth-login`
  resolve a name via the pure `cli_account_resolve`
  (`Found`/`SeedThenUse`/`NotFound`); a name absent from the DB but present in
  `config.toml` is seeded via `create_account` + the shared
  `account_seed.account_create_kwargs` mapping (CLI helper
  `cli._resolve_account_row`). `remove-account` is **secrets-only by default**
  (DB row untouched, back-compat); `--delete-row` deletes the row, `--force`
  cascades when messages reference it. One-shot `sync` (bare) iterates
  `list_syncable_accounts` like the daemon; `--account NAME` resolves via
  `get_account_by_name` and syncs even a paused (`sync_enabled = FALSE`)
  account, rejecting archive accounts. `backfill-internal-date` remains
  TOML-driven (`_account_or_die`) — out of 2A.2d scope. No new migration.
- **`sync_enabled` CLI setter (follow-up to 2A.2):** `enable-account NAME` /
  `disable-account NAME` toggle `accounts.sync_enabled` via the pure planner
  `cli_sync_toggle.plan_sync_toggle` (reject / noop / apply). Name resolution is
  DB-only (no TOML seed — toggling presupposes the row exists); archive rows are
  rejected (the daemon never syncs them); an account already in the target state
  is a no-op that leaves `updated_at` untouched. Both commands share the
  `cli._apply_sync_toggle` helper, which only calls `update_account` on the
  `apply` branch. No new migration (`sync_enabled` ships in `0020`).
- **Account CRUD admin screens (Sub-plan 2A.3, shipped — closes #125):**
  server-rendered HTMX screens at `/admin/accounts` for list, create, edit,
  delete, store-password, test-connection, enable/disable sync, and Gmail OAuth
  "Connect". Code: thin HTML router
  [`serve/admin/accounts_panel_router.py`](src/localmail/serve/admin/accounts_panel_router.py)
  (~330 lines) + pure form logic
  [`serve/admin/account_forms.py`](src/localmail/serve/admin/account_forms.py)
  (unit-tested in isolation via `tests/test_account_forms.py`). Templates under
  `serve/admin/templates/accounts/` (`list.html`, `form.html`,
  `_form_fields.html`, `_row.html`, `_test_result.html`, `_secret_status.html`,
  `_delete_confirm.html`);
  auth-method field toggle in the served static file
  [`serve/admin/static/accounts-panel.js`](src/localmail/serve/admin/static/accounts-panel.js)
  (CSP `script-src 'self'`, no inline JS). Each mutating action carries a
  **method-bound** CSRF token via `csrf_token_for_method` — the explicit closure
  of #125 (the shared mint from 2B.5 now has its first non-daemon consumer).
  Backend change: `probe_connection` now supports `oauth2` accounts — threads
  `gmail_client_secrets_file` into the existing XOAUTH2 path; a missing refresh
  token surfaces as a clean `AccountFieldError` (→ inline error fragment), never a 500.
  Validation errors render **inline beside the offending field** (`_form_fields.html`,
  HTTP 400 + HTMX swap); successful create/update returns `HX-Redirect` to the
  edit page. On OAuth completion the callback redirects to
  `/admin/accounts/{id}?oauth=success`. Archive accounts are rejected by
  test-connection (same as before). **No new migration** (reuses `sync_enabled`
  from `0020`).
- **Friendly test-connection failures (#158, resolved):** a *genuine* connect
  failure (wrong host/port/password, DNS, TLS) raises `OSError` /
  `imaplib.IMAP4.error` / `imapclient.exceptions.IMAPClientError`, which used to
  escape both `probe_connection`'s narrow `except RuntimeError` and the routes'
  `except AccountFieldError` as a **500**. The classification tuple
  `accounts.CONNECT_FAILURE_EXC_TYPES` names exactly those types and lives next
  to `probe_connection`; the service still does **not** catch it (its contract is
  to raise on connect failure — the broadening is deliberately **at the
  transport routes**). The HTML route
  (`accounts_panel_router.py::test_connection`) renders the `_test_result.html`
  error fragment (HTTP 200, `ctx["error"]`); the JSON `/v1` route
  (`accounts_router.py::test_connection`) mirrors it as a clean **400** with the
  error detail (uniform with the existing `AccountFieldError → 400` mapping).
  Both paths keep `probe_connection`'s builtin transient-classification
  narrowness intact.
- **User-management admin screens (Sub-plan 2A.4, shipped):** server-rendered
  HTMX screens at `/admin/users` + a JSON `/v1/admin/users` router, sharing one
  service layer
  [`src/localmail/api/admin/users.py`](src/localmail/api/admin/users.py):
  list/create/delete users, per-account ACL grant/revoke (a checklist over every
  account on the edit screen), `is_admin` toggle, admin session revocation,
  admin password reset (no old password), and enable/disable (`disabled_at`).
  Two lock-out guards: the **count-based last-admin** rule lives in the service
  (the pure `would_orphan_last_admin` predicate + an IO wrapper reading
  `count(*) WHERE is_admin IS TRUE AND disabled_at IS NULL`; raises
  `LastAdminError`), and the **identity-based self-action** rule (no self-demote,
  no self-delete) lives in the routers (compared `uid == admin.id`, returns
  **409**). Both guards map to **409** (mirroring the accounts cascade-refuse
  409); validation maps to **400**. The edit screen also renders unsafe controls
  `disabled` server-side via `action_flags` — UX only; a hand-crafted POST still
  hits the guards. Pure form logic in
  [`serve/admin/user_forms.py`](src/localmail/serve/admin/user_forms.py)
  (unit-tested in `tests/test_user_forms.py`). Method-bound CSRF throughout (a
  PATCH token can't replay on DELETE). **No new migration** — reuses
  `is_admin`/`disabled_at`/`sessions_invalidated_at` + `user_accounts` (0016).
  Closes the `/admin/users` 404.
- **Imports admin screens (Sub-plan 2A.5, shipped):** server-rendered HTMX
  screens at `/admin/imports` + a JSON `/v1/admin/imports` router, sharing the
  service layer
  [`src/localmail/api/admin/imports.py`](src/localmail/api/admin/imports.py):
  list/create/cancel import jobs, per-job status, and `reconcile_orphaned_jobs`
  (called at serve startup to move any `running` jobs left over from a crash into
  `failed`). Closes the last 404 admin nav link (`/admin/imports`).
  The new `src/localmail/importer/` package contains:
  `paths.py` (`resolve_import_path` — config-allowlist guard using `realpath`;
  empty `roots` = imports disabled, raises `ImportNotAllowed`),
  `sources.py` (`iter_mbox`/`iter_maildir` → `ImportedMessage` named-tuples;
  received-date from the mbox `From_` envelope line for mbox sources, maildir
  file mtime for maildir sources),
  `job_state.py` (pure predicates `is_stale`/`is_terminal`/`should_checkpoint`),
  `runner.py` (`run_import` — streams a source through `sync.process_one_message`
  with per-message SAVEPOINT isolation, periodic progress flush +
  `last_progress_at` heartbeat, cooperative cancel via the `cancel_requested`
  column, and guaranteed terminal status write on exit).
  Migration `0026_import_jobs.sql` adds the `import_jobs` table (columns:
  `id`, `account_id`, `source_kind`, `source_path`, `status`, `inserted`,
  `skipped`, `failed`, `error_msg`, `created_at`, `last_progress_at`) plus a
  partial unique index `ON import_jobs ((TRUE)) WHERE status IN ('pending','running')`
  — the single-active busy-guard that prevents two concurrent imports.
  Imports target a pre-created **archive** account (dropdown on the create form);
  the service layer is admin-global (NOT per-user ACL-scoped, consistent with the
  accounts and users admin services). Source paths must reside under a directory
  in `[imports].roots` (empty = imports disabled); paths are resolved server-side
  only. Received date from the source (mbox envelope / maildir mtime) is stored
  in `messages.internal_date`. Three-layer mid-import failure visibility: runner
  sets terminal `failed` + `error_msg` on unhandled errors; `last_progress_at`
  stall detection (panel shows red past `[imports].stale_seconds`); and
  `reconcile_orphaned_jobs` at serve startup clears any `running` rows from a
  prior crash. The import worker runs in-serve as a plain thread started by
  `start_job`; `localmail import <path> --account NAME --kind {mbox,maildir}`
  invokes the same `run_import` synchronously from the CLI. Re-import is
  idempotent — already-imported messages are skipped via the existing per-account
  Message-Id / raw-SHA256 dedup. **Migration `0026_import_jobs.sql`** (2A.5).
  **Checkpoint cadence (#163, resolved):** the runner used to flush progress +
  poll cancel only on `c.processed % checkpoint_every == 0`, so a sub-`checkpoint_every`
  import showed `0/0/0/0` until the terminal write and its Cancel button was inert,
  and a small-count-but-slow import (few huge attachments) was unresponsive for a
  long time. The flush/poll decision now lives in the pure predicate
  `job_state.should_checkpoint(processed, processed_at_last_checkpoint,
  seconds_since_checkpoint, checkpoint_every, checkpoint_seconds)`, which fires on
  three independent triggers: the **first** processed message (immediate progress +
  cancellability), the **count** cadence (`checkpoint_every`, unchanged), and a new
  **time** cadence (`[imports].checkpoint_seconds`, default 2 — decouples
  responsiveness from per-message cost). `<= 0` disables a cadence; the first-message
  flush always fires. `run_import` tracks `processed_at_last_checkpoint` +
  `last_checkpoint_at` and takes an injectable `clock` (default `time.monotonic`)
  so the time branch is deterministically unit-tested. `checkpoint_seconds` threads
  from config through `start_job` and all three callers (CLI, JSON router, HTML panel
  router). No new migration.
  **Concurrent-CLI-safe reconcile (#162, resolved):** `reconcile_orphaned_jobs`
  ran at serve startup and flipped **every** active (`pending`/`running`) row to
  `failed`, on the assumption that an active row could only be an orphaned
  in-serve worker thread. But `localmail import` runs the same `run_import`
  **synchronously in a separate process** with its own `running` row — so a serve
  restart mid-CLI-import clobbered the live job's status *and* released the
  single-active busy-guard (`import_jobs_single_active_uniq`), opening a window
  for a panel-initiated import to run concurrently. Migration
  `0027_import_jobs_owner.sql` adds nullable `owner_host` / `owner_pid`, recorded
  at `create_job` time — the creating process is the running process for both the
  CLI (one process) and the in-serve panel (the worker thread runs in the serve
  process), so `os.getpid()` at create is the pid whose liveness reconcile must
  check. `reconcile_orphaned_jobs(conn, *, current_host=None, pid_alive=...)` now
  selects active rows and reaps one only when its owner is verifiably gone, via
  the pure predicate `importer/ownership.py::should_reap` (reap iff `owner_pid IS
  NULL` — legacy/never-started; else keep when `owner_host != current_host`; else
  reap iff `not pid_alive`). `pid_is_alive` is the single liveness syscall
  (`os.kill(pid, 0)`), isolated so `should_reap` stays pure and unit-tested;
  `current_host` / `pid_alive` are injectable for deterministic DB tests. A live
  CLI import (pid alive) now survives a serve restart, keeping the busy-guard
  held; orphaned serve **and** CLI jobs (pid dead) are still reaped. **Accepted
  limitation:** pid reuse can rarely keep a dead job's row until the next restart
  (self-heals; low probability on single-host). **Migration
  `0027_import_jobs_owner.sql`** (#162).
- **DaemonSupervisor + HTTP + CLI (Sub-plan 2B.4, shipped):** two control
  planes for the sync daemon. **Plane B** (process lifecycle) lives in
  [src/localmail/serve/daemon_supervisor.py](src/localmail/serve/daemon_supervisor.py):
  `DaemonSupervisor` owns `localmail run` via `subprocess.Popen`
  (`start`/`stop`/`restart`/`status`/`recent_log_lines`), a state machine
  `stopped → starting → running → stopping → stopped` with `crashed` for an
  unexpected child exit (detected by the stdout reader thread hitting EOF while
  state is still `running`), and a bounded ring buffer (`deque(maxlen)`) of the
  child's combined stdout/stderr. `stop()` is SIGTERM → wait
  `daemon.shutdown_grace_seconds` → SIGKILL, and deliberately **releases the
  lock before waiting** so the reader thread can never deadlock against the
  grace wait. `ExternalDaemonSupervisor` is the stub for
  `[serve] supervise_daemon = false` (systemd deploy): `status()` reports
  `external`; lifecycle ops raise `SupervisorUnavailable`. Pure helpers
  (`resolve_runtime_dir`, `socket_path`, `default_daemon_argv`,
  `status_to_dict`) are shared by serve + CLI so both derive the same socket
  path / launch argv / wire shape. The child is launched as
  `python -m localmail run` (portable — `src/localmail/__main__.py` shim, no
  PATH dependence). The **control socket**
  ([src/localmail/serve/daemon_control_socket.py](src/localmail/serve/daemon_control_socket.py))
  is newline-delimited JSON over a Unix socket at
  `${runtime_dir}/localmail-supervisor.sock` (mode 0600): `handle_control_request`
  is a pure dispatcher (supervisor in, dict out, never raises),
  `ControlSocketServer` wraps it with an accept loop, `send_control_request` is
  the client half the CLI uses. `create_app` builds the supervisor on
  `app.state.daemon_supervisor` (real when `supervise_daemon`, stub otherwise)
  **side-effect-free** — the child spawns only on an explicit `start()`, and the
  control socket binds only in the lifespan when `enable_control_socket=True`
  (the `serve` CLI path), so TestClient apps never bind a shared socket. HTTP
  routes ([src/localmail/serve/admin/daemon_router.py](src/localmail/serve/admin/daemon_router.py),
  admin-gated, method-bound CSRF): `GET /v1/admin/daemon` fuses supervisor
  process state + `daemon_heartbeats` + recent log (`supervise_daemon_externally`
  derives from the supervisor's own `state == external`, not config, so a
  swapped stub reports correctly); `POST /v1/admin/daemon/{start,stop,restart}`
  (Plane B; 409 on the external stub); `POST /v1/admin/daemon/reload` and `POST
  /v1/admin/accounts/{id}/restart-sync` (Plane A → `enqueue_command` reusing 2B.3,
  not re-implemented; restart-sync 404s an unknown account before enqueue). CLI
  ([src/localmail/daemon_cli.py](src/localmail/daemon_cli.py), registered via
  `main.add_command(daemon_group)`): `localmail daemon {status,reload,restart-account}`
  work against the DB planes even when externally supervised;
  `{start,stop,restart}` go over the socket and exit non-zero with a clear note
  when `supervise_daemon=false` (external) or the socket is unreachable (serve
  not running). `status` always prints heartbeats; an unreachable socket is
  reported, not a failure. **No new migration** (reuses 0023 heartbeats + 0024
  commands).
- **Async lifecycle + admin panel (Sub-plan 2B.5, shipped — closes the 2B arc):**
  lifecycle ops no longer block a request/socket worker (#146). `DaemonSupervisor`
  grows `request_start()`/`request_stop()`/`request_restart()` that set the
  **transitional** state synchronously (`starting`/`stopping`) under `_lock`,
  then run the existing blocking `start()`/`stop()`/`restart()` body on **one
  dedicated lifecycle thread**; a second lifecycle op while one is in flight
  raises `SupervisorUnavailable` (the **busy-guard**, keyed on
  `_lifecycle_thread.is_alive()`, not state). The blocking variants stay (used by
  `close()` on serve shutdown — teardown must block — and by tests).
  `ExternalDaemonSupervisor` has matching `request_*` stubs that raise.
  `DaemonSupervisorT = DaemonSupervisor | ExternalDaemonSupervisor` is the shared
  param type. HTTP `POST /v1/admin/daemon/{start,stop,restart}` now call
  `request_*` and return **202** with the transitional status; the busy-guard /
  external stub both surface as **409**. The control socket dispatcher and the
  `localmail daemon {start,stop,restart}` CLI likewise use `request_*`; the CLI
  **polls `status` until the op settles** (`running`/`stopped`) — `--no-wait`
  skips the poll. CLI poll constants live in `daemon_cli.py`
  (`_LIFECYCLE_POLL_INTERVAL_S`, `_START_SETTLE_TIMEOUT_S`, reuses
  `_LIFECYCLE_TIMEOUT_BUFFER_S` + `_STATUS_TIMEOUT_S`). The GET-route fusion is
  extracted into `daemon_router.build_daemon_view(supervisor, conn, *,
  stale_seconds)` — the single source shared by the JSON route and the HTML
  panel. **Admin panel** at `/admin/daemon`
  ([src/localmail/serve/admin/daemon_panel_router.py](src/localmail/serve/admin/daemon_panel_router.py),
  mounted at `/admin`): a full page + a self-polling HTMX partial at
  `/admin/_partials/daemon-status` (the `#daemon-status` div re-carries its
  `hx-get`/`hx-trigger="every {{DAEMON_PANEL_POLL_SECONDS}}s"` after each
  `outerHTML` swap). Status table is red past `heartbeat_stale_seconds` (server
  `stale` flag, no client clock); lifecycle buttons are **disabled when
  `supervise_daemon_externally`**; Plane-A reload + per-account restart-sync
  buttons stay enabled. Each mutating control carries its own **method-bound**
  CSRF token via the reusable
  [serve/admin/csrf.py](src/localmail/serve/admin/csrf.py)`::csrf_token_context`
  helper (returns `csrf_token_for` legacy single-arg + `csrf_token_for_method`
  — the latter is the shared #125 mint, consumed by the account screens in 2A.3).
  Restart-sync buttons are deduped per account (idle+poll workers share one).
  The `/v1/admin/*` endpoints stay pure machine-JSON (no HTMX content
  negotiation); the panel polls the dedicated HTML partial. **No new migration.**
  **2B.5 follow-ups resolved (#148, #149):** the panel's mutating buttons use
  `hx-swap="none"`, so a rejected control (busy-guard **409**, CSRF **400**)
  used to look inert; the served static
  [admin/static/daemon-panel.js](src/localmail/serve/admin/static/daemon-panel.js)
  now binds an `htmx:afterRequest` listener (filtered to `verb === "post"` so
  the 2s status poll doesn't toast) that surfaces a transient toast in the
  `#daemon-toast` region. That region lives in `daemon/panel.html` **outside**
  the self-swapping `#daemon-status` fragment so the poll's `outerHTML` swap
  can't wipe an in-flight message. The JS is a served file (not inline / not an
  htmx `hx-on::`) because the `/admin` CSP is `script-src 'self'` with no
  `unsafe-inline`/`unsafe-eval`. **#149:** `DaemonSupervisor.close()` sets a
  `_closing` flag under `_lock` before its blocking `stop()`, and `start()`
  checks it under `_lock` as the single spawn chokepoint — so an async
  `request_restart` caught between its `stop()` and `start()` halves at serve
  shutdown can no longer re-spawn an orphaned child. The flag-set and the spawn
  are serialised by `_lock`: start() either sees the flag and skips, or spawned
  first and close's stop() reaps it.
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
- **Hard ACL clamp inside the Searcher**: the ACL is enforced in **two**
  places, and both are load-bearing. `api/search.py::_scope_filters_by_acl`
  intersects the caller's *structured* `account_ids` filter and
  short-circuits an empty intersection to an empty page; but the ACL then
  travels to the Searcher as `account_id:` **DSL tokens in the query
  string**, and `parse_query` unions every `account_id:` token regardless of
  origin — so a token smuggled through the untrusted free text OR-widened
  `m.account_id = ANY(...)` past the grant. `Searcher.search` therefore takes
  `allowed_account_ids: list[int] | None` and pipes `parsed` through
  `_clamp_account_ids_to_acl` **after any smart rewrite and before every
  retrieval branch** (hybrid pool, `sort=date` lexical keyset, empty-query
  fallback), so the cached pool inherits the clamped filter and
  `continue_page` / `grow_pool` stay scoped without re-clamping. `None` means
  "no ACL" (CLI / local callers keep full DSL power); an **empty list** is a
  real grant-nothing ACL. Two traps to preserve:
  - **An empty id set collapses to `_NO_ACCOUNT_SENTINEL = -1`, never `[]`.**
    `_filter_sql` treats an empty list as falsy and drops the clause
    entirely — i.e. *all accounts*, the exact inverse of the intent. The same
    trap bit `_resolve_account_names` (all `account:NAME` values unknown →
    `accounts=[]` → matched everything while logging "matching no rows"); it
    uses the same sentinel now.
  - **Only `account_ids` is clamped, deliberately.** `account:NAME` resolves
    into the separate `filters.accounts` field, which `_filter_sql` emits as
    its own `AND` clause — it can only intersect, never widen. Same for
    `folder_id:`, whose union is bounded by the account clause. Tests in
    `tests/test_search_acl_clamp.py`.
- **Server-side subscription cursors on `/v1/changes`**: migration
  `0032_channel_subscriptions.sql` adds `channel_subscriptions`
  (one row per `(user_id, name)`, `cursor BIGINT`, FK to
  `api_users` `ON DELETE CASCADE`). `GET /v1/changes?subscription=<name>`
  reads the stored cursor instead of a client-supplied `since` (the two
  are **mutually exclusive**, 400 if both are given);
  `POST /v1/changes/ack {"subscription","cursor"}` → 204 advances it.
  Lets a polling client be stateless — poll, process, ack — instead of
  re-reading the 200-message tail after every restart. Invariants, each
  with a test in `tests/test_serve_changes_route.py`:
  - **A fresh subscription primes at the current tip, not the backlog**,
    so a first-time subscriber never replays old mail as new work.
    The tip is `_current_tip` — `MAX(id)` **with the same
    `changes_safe_horizon_s` filter the `since` branch applies**. Using a
    raw `MAX(id)` here would be a silent permanent-loss bug: a tx that
    allocated a lower id can commit after one that allocated a higher id,
    so the cursor could start past a not-yet-visible message that no later
    poll would ever return. Note the horizon **bounds** this window rather
    than closing it — `date_received` defaults to the *transaction*
    timestamp and `sync_mailbox` commits per 50-message batch, so a batch
    slower than the horizon still races (pre-existing, applies equally to
    `since`).
  - **Acks are monotonic** (`GREATEST` in the upsert), so a stale or
    replayed ack cannot resurface processed messages.
  - **Creation is atomic.** `_claim_subscription` uses `ON CONFLICT
    (user_id, name) DO NOTHING RETURNING cursor` and the loser of a race
    re-reads the winner's cursor. A bare INSERT here raised
    `UniqueViolation` on two simultaneous first polls, which escaped as a
    **500** (only `APIError` subclasses reach the problem+json handler).
  - **An ack past the archive's highest `messages.id` is rejected (400).**
    Because acks are monotonic there is no API path back, so an
    out-of-range value (a timestamp, an overflowing BIGINT → a raw
    `NumericValueOutOfRange` 500) would silence the subscription for good.
    The bound comes from `_max_message_id`, which is deliberately
    **global — no ACL, no horizon** — because Postgres rewrites `MAX(id)`
    on the PK into a one-row `Index Only Scan Backward`. Do not "tighten"
    it to reuse `_current_tip`: that plan is an index scan over all of the
    caller's rows, acceptable once per subscription but not on every ack.
  - **Row growth is capped** at `serve.max_subscriptions_per_user`
    (default 32) on both the GET and the ack create paths, since a client
    deriving the name from a UUID would otherwise grow the table without
    bound. Advisory only — concurrent creates at the cap can overshoot by
    one; it is a resource guard, not a security boundary.

  Known gaps, filed not fixed: the SQL lives in `serve/routes/changes.py`
  rather than `api/`, so **MCP tools cannot use subscriptions** (#224); there
  is no reset/delete endpoint and the first `GET` has a write side effect
  (#225); and the safe-horizon precondition above is undocumented on the
  `since` path too (#227).

## MCP server (search Phase 3)

Remote, multi-user MCP server exposing the archive's read surface to AI
agents. Mounted into the existing `serve` FastAPI app at `/mcp` over
**Streamable HTTP** — no new listener; TLS and the `--no-tls`/`--bind
127.0.0.1` rules inherit from `serve` unchanged. Endpoint URL:
`https://<host>:<port>/mcp`. Operator/agent guide:
[docs/mcp-usage.md](docs/mcp-usage.md). No new migration (reuses
`api_users` / `api_tokens` / `user_accounts`).

- **Auth = opaque bearer reusing `api_tokens`.** `LocalmailTokenVerifier`
  ([src/localmail/mcp/auth.py](src/localmail/mcp/auth.py)) wraps the existing
  `api.auth.verify_token` and carries the user id in `AccessToken.subject`. The
  sync DB lookup is offloaded via `anyio.to_thread.run_sync` so the verifier
  never blocks the event loop. Agents get a token from `POST /v1/auth/login`
  (refresh: `/v1/auth/refresh`) and pass `Authorization: Bearer <token>` to
  `/mcp` — there is **no** OAuth authorization-server flow; clients configure the
  token directly.
- **Five ACL-scoped read tools** in
  [src/localmail/mcp/{server,tools,auth}.py](src/localmail/mcp/server.py), each
  calling `localmail.api` accessors directly (no HTTP hop, **no `wire.py`** — the
  api/ layer already returns the wire-shaped dicts, so HTTP routes and MCP tools
  share that serialization). Per-user ACL applies to every tool (results scoped
  to the token user's granted accounts).
  - `search(query, sort="rank"|"date", limit, cursor, account_ids, folder_ids,
    date_from, date_to, from_addr, to, subject, has_attachment, lang, smart)` —
    hybrid search; `smart=true` runs the Phase-4 LLM rewrite (page 1) and the
    response `rewrite_skipped` reflects whether it happened; page by re-calling
    with `next_cursor`; a cursor-expired error means re-run without a cursor.
  - `get_message(message_id, full_headers=False)`.
  - `get_attachment(sha256, mode="text"|"metadata")` — extracted text or
    metadata, **never raw bytes** (raw download stays the HTTP
    `/v1/attachments/{sha256}` route).
  - `list_messages(account_ids, folder_ids, limit, cursor)` — keyset
    date-ordered browse, newest first.
  - `list_accounts()`.
- **Wiring**: `FastMCP(token_verifier=…, auth=AuthSettings(issuer_url,
  resource_server_url, required_scopes=[]), stateless_http=True,
  json_response=True, streamable_http_path="/")`, mounted at `/mcp` in
  `create_app` (gated by `enable_mcp` **and** the importable `[mcp]` extra; if
  the extra is absent, `serve` runs and logs an INFO skip line). The session
  manager is started in the app lifespan (`async with
  mcp_server.session_manager.run()`).
- **Config** `McpConfig` (`localmail.config`, `[mcp]`): `enabled` (default
  false), `issuer_url` / `resource_server_url` (default
  `http://localhost:8443`; advertised in the SDK's OAuth resource-metadata —
  opaque-bearer clients ignore them). `serve` CLI forwards `cfg.mcp`.
- **Three design reconciliations vs the spec**: (1) no `wire.py` (shaping
  already lives in api/); (2) ONE `search` tool, not three — `run_search` takes a
  single optional `cursor` and auto-grows the pool, paging = re-call with
  `next_cursor`; (3) `get_message(full_headers=…)`, not
  `include_body`/`include_attachments`.
- Tools return structured content; `SearchCursorExpired` / `NotFound` /
  `ValidationFailed` map to clean `ToolError`s. Raw attachment bytes are
  intentionally NOT exposed over MCP (HTTP `/v1/attachments` only). **Deferred
  follow-ups**: full OAuth 2.1 **authorization server** (`/authorize`, `/token`,
  dynamic client registration) — the *discovery surface* half of "Approach B"
  is now shipped (see next bullet); richer per-tool docstrings.
- **RFC 9728 protected-resource discovery (shipped — "Approach B" discovery half):**
  a spec-strict MCP client can discover `/mcp` as a protected resource without
  localmail becoming an OAuth authorization server (it stays opaque-bearer;
  tokens come from `/v1/auth/login` out-of-band). The pure module
  [src/localmail/mcp/discovery.py](src/localmail/mcp/discovery.py) holds
  `MCP_MOUNT_PATH`/`RESOURCE_NAME`, `mcp_resource_url(base)` (origin + `/mcp`,
  trailing-slash-safe), `resolve_authorization_servers(configured, issuer)`
  (`configured or [issuer_url]`), and the one SDK-touching wrapper
  `build_protected_resource_routes(config)` (function-level SDK import so the
  module stays import-safe). Two halves make the surface reachable: (1)
  `build_mcp_server` passes `AnyHttpUrl(mcp_resource_url(...))` as
  `AuthSettings.resource_server_url`, so the SDK's 401 `WWW-Authenticate`
  challenge advertises the canonical root URL
  `/.well-known/oauth-protected-resource/mcp`; (2) `create_app` registers the
  SDK's `create_protected_resource_routes` on the **top-level** app (public,
  via `_try_build_mcp` → `app.router.routes.extend(...)`) at that exact path —
  the SDK's own sub-mounted copy lands at the non-canonical
  `/mcp/.well-known/oauth-protected-resource/mcp` and is left alone (harmless).
  New config `McpConfig.authorization_servers: list[AnyHttpUrl] | None = None`
  (operator-configurable; defaults to `[issuer_url]`). `resource_server_url`
  stays the bare public origin (no `/mcp`; appended internally). No migration,
  no new dependency. Design:
  [docs/superpowers/specs/2026-06-10-mcp-protected-resource-discovery-design.md](docs/superpowers/specs/2026-06-10-mcp-protected-resource-discovery-design.md).
- **OAuth 2.1 authorization server (opt-in, shipped):** localmail can act as an
  OAuth AS so spec-strict MCP clients self-onboard via browser login + consent —
  no hand-pasted bearer token. Enabled with `[mcp] authorization_server_enabled =
  true`; requires `[serve] state_signing_key` (>= 32 chars — `create_app` fails
  loud at startup without it). The AS issuer is **auto-derived** as
  `<resource_server_url>/mcp` in `_try_build_mcp` (zero-config for the operator;
  an explicit `[mcp] authorization_servers` override is still honoured for
  pointing at an external IdP). Code sub-packages:
  `src/localmail/mcp/oauth/` — `consent_state.py` (HMAC-signed state token),
  `consent_forms.py` (pure login/consent form logic), `clients.py` (DCR
  registration + unused-client cleanup), `codes.py` (authorization code issue +
  exchange), `refresh.py` (sliding refresh token rotation), `access.py`
  (access token issue), `provider.py` (`load_access_token` wraps the existing
  `verify_token` so the ACL is unchanged), `registration.py` (per-IP rate-limit
  guard); `src/localmail/serve/oauth/` — `consent_router.py`
  (`/oauth/consent` login + allow/deny screens), `registration_guard.py`
  (per-IP middleware). Access tokens are stored in the existing `api_tokens`
  table (`provider.load_access_token` wraps `verify_token`) — the per-user ACL
  and `grant-account` grants are unchanged. Refresh tokens are sliding-rotated:
  each refresh resets the 30-day clock (`oauth_refresh_token_ttl_s`); a browser
  re-login is required only after ~30 days of inactivity, on revocation, or if
  the api_user is disabled. The consent login reuses the `/v1/auth/login`
  rate-limit + `DUMMY_PASSWORD_HASH` timing-parity protections. Open DCR (`POST
  /register`) is bounded by a per-IP rate-limit middleware
  (`oauth_registration_max` per `oauth_registration_window_s`, default 20/hour)
  and unused-client cleanup (`oauth_client_unused_retention_s`, default 24h).
  **Known limitations:** AS metadata is served at the OIDC-style path-suffix
  form `<origin>/mcp/.well-known/oauth-authorization-server`; the strict RFC 8414
  §3.1 insertion form `<origin>/.well-known/oauth-authorization-server/mcp` is
  NOT served (real MCP clients use the path-suffix form). Migration
  `0028_oauth_server.sql` adds `oauth_clients`, `oauth_authorization_codes`,
  `oauth_refresh_tokens`, `oauth_registration_attempts`, and nullable
  `api_tokens.oauth_client_id`. No new uv dependency (`mcp` extra already
  provides the AS machinery). Design:
  [docs/superpowers/specs/2026-06-15-mcp-oauth-authorization-server-design.md](docs/superpowers/specs/2026-06-15-mcp-oauth-authorization-server-design.md).
- **AS hardening tidy-ups (#182 review follow-ups M1/M2/M3, shipped):**
  - **M1 — disabled-user refresh containment:** `refresh.load_refresh` JOINs
    `api_users` and filters `disabled_at IS NULL` (mirroring `api.auth.verify_token`),
    so a disabled user's refresh token is treated as non-existent — both the SDK's
    `load_refresh_token` and `rotate_refresh` reject it. RFC 9700 §4.13. If the
    user is disabled in the window *between* the SDK's load and exchange,
    `provider._exchange_refresh_sync` fails closed with a `TokenError`
    (`invalid_grant`) raised after the connection context exits — it no longer
    asserts on the `None` rotation (which would have been an HTTP 500).
  - **M2 — broadened unused-client cleanup:** `clients.cleanup_unused` now reaps a
    client when it has **no unexpired refresh token** *and* its last activity
    (`COALESCE(last_used_at, created_at)`) is older than the retention window —
    covering once-used-then-idle clients, not just never-used ones. The
    `NOT EXISTS` live-refresh-token guard means an actively-refreshing client is
    never reaped (reaping its row would break the next `get_client`).
  - **M3 — DCR rate-limit proxy peeling:** `RegistrationRateLimit` takes an
    `auth_config` and resolves the client IP via the new pure
    `registration_guard.resolve_scope_client_ip` → shared `api.client_ip.resolve_client_ip`,
    so the per-IP `/register` cap peels `X-Forwarded-For` against
    `auth.trusted_proxies` exactly like the login limiter (empty config = socket
    peer, unchanged). Wired in `create_app` (`auth_config=auth_cfg`).
  - No new migration for M1/M2/M3.
- **Refresh-token family revocation on reuse (#183, #185, shipped):** rotation no
  longer hard-deletes the presented refresh token. Migration
  `0029_oauth_refresh_token_family.sql` adds `oauth_refresh_tokens.family_id`
  (`UUID NOT NULL DEFAULT gen_random_uuid()` — existing rows become singleton
  families) + `consumed_at TIMESTAMPTZ` (NULL = live; set = rotated tombstone),
  plus indexes on `family_id` and `client_id` (the latter is **#185**, serving
  `cleanup_unused`'s correlated `NOT EXISTS`). `refresh.rotate_refresh` now
  returns a `RotateResult(outcome, new_token)` enum: it **tombstones** the
  presented token (UPDATE `consumed_at`) and mints a successor in the **same
  family** (`outcome="rotated"`); replaying an already-consumed token is reuse
  (a stolen-copy signal, RFC 9700 §4.14.2) → it `DELETE`s the **whole family**
  and returns `outcome="reuse"`; an absent/expired/disabled-user token is
  `outcome="unknown"` (natural, never nukes the family — the M1 disabled-user
  containment now lands here). `refresh.load_refresh` filters
  `consumed_at IS NULL` so tombstones never load as live;
  `refresh.sweep_consumed` GCs consumed tombstones past their own `expires_at`
  (opportunistic, called on the rotation path — reuse stays detectable for the
  token's full lifetime). `clients.cleanup_unused`'s live-token guard gained
  `AND r.consumed_at IS NULL` so a not-yet-expired tombstone can't keep an
  abandoned client alive (the M2 interaction). `provider._exchange_refresh_sync`
  switches on the outcome: `reuse` commits the family DELETE, logs a WARNING
  (`refresh-token reuse detected; revoked family for client_id=…`, no token
  leakage), and raises `TokenError("invalid_grant")`; `unknown` rolls back and
  raises. **Concurrency:** the tombstone UPDATE carries an
  `AND consumed_at IS NULL` guard + `rowcount == 1` claim check, so two
  concurrent rotations of the same live token are serialised by the row lock —
  exactly one claims it and mints a successor; the loser's guarded UPDATE
  matches 0 rows (the token was consumed out from under it = a reuse signal) and
  revokes the family. No double-successor, no `SELECT FOR UPDATE` needed.
  Design:
  [docs/superpowers/specs/2026-06-16-oauth-refresh-token-family-revocation-design.md](docs/superpowers/specs/2026-06-16-oauth-refresh-token-family-revocation-design.md).
- **Access-token family containment on reuse (closes the prior accepted
  limitation):** the family DELETE used to revoke refresh tokens only — access
  tokens already minted along the chain lived in `api_tokens` with no family
  correlation and stayed valid at `/mcp` until their ≤1h TTL. Migration
  `0030_api_tokens_refresh_family.sql` adds nullable
  `api_tokens.oauth_refresh_family_id` (UUID, partial index `WHERE … IS NOT
  NULL`). OAuth-minted access tokens are tagged with their refresh family
  (`access.mint_access(family_id=…)` — the code-exchange path reads the family
  via `load_refresh` after minting the refresh token; the rotation path reuses
  the `row.family_id` it already loads). On reuse detection
  (`RotateResult.family_id`, populated on the `reuse` outcome) the provider's
  reuse branch calls `access.revoke_access_family(family_id)` **inside the same
  transaction** as the refresh-family DELETE and **before** the commit, so both
  purges are atomic; the reuse WARNING gains `(access tokens purged=%d)`.
  Reuse-only — normal rotation predecessors still expire by their ≤1h TTL (eager
  revocation would break in-flight requests). Login tokens (`/v1/auth/login`,
  `oauth_refresh_family_id IS NULL`) are structurally immune to the family purge.
  `refresh.py` still touches only `oauth_refresh_tokens` (it reports `family_id`
  as data); `access.py` owns `api_tokens`; the provider orchestrates both. Design:
  [docs/superpowers/specs/2026-06-16-access-token-family-containment-design.md](docs/superpowers/specs/2026-06-16-access-token-family-containment-design.md).
- **RFC 8707 resource indicators (shipped):** `/authorize` validates the
  client's `resource` against a configurable accepted set
  (`McpConfig.resource_indicators`, default
  `[mcp_resource_url(resource_server_url)]`) via the pure
  `mcp/oauth/resource_indicator.py`
  (`canonicalize_resource`/`resolve_accepted_resources`/`decide_resource`); the
  bound resource is carried through the consent blob →
  `oauth_authorization_codes.resource` → onto the minted access
  (`api_tokens.oauth_resource`) + refresh (`oauth_refresh_tokens.resource`)
  tokens, and enforced at `/mcp` in `access.load_access` (NULL = unrestricted;
  `/v1` REST unchanged). A missing `resource` is accepted (and bound to the
  first accepted resource) unless `oauth_require_resource_indicator = true`, in
  which case it's rejected with `invalid_request`. Migration
  `0031_oauth_resource_indicator.sql` adds the three `resource`/`oauth_resource`
  columns. **Accepted SDK limitations:** the SDK swallows the token-endpoint
  `resource` (validated at authorize time only) and lacks an `invalid_target`
  error code (a bad resource → `invalid_request`).
- **Integration test** [tests/test_mcp_integration.py](tests/test_mcp_integration.py):
  runs uvicorn in a thread + a real `mcp` client over Streamable HTTP, asserting
  the 5-tool list + ACL scoping (marked `integration`, skipped if the `mcp`
  client isn't installed).

## Desktop GUI admin mode (`gui/`, phases 2+3+4 shipped)

The Tauri 2 + Svelte 5 client gained an operator/admin mode gated on
`is_admin`. Design:
[docs/superpowers/specs/2026-07-23-admin-mode-tauri-gui-design.md](docs/superpowers/specs/2026-07-23-admin-mode-tauri-gui-design.md);
phase 1 (backend bearer auth) shipped in PR #203, phases 2+3 (frontend shell
+ Accounts panel) in the plan
[docs/superpowers/plans/2026-07-24-admin-mode-gui-phase2-3.md](docs/superpowers/plans/2026-07-24-admin-mode-gui-phase2-3.md),
phase 4 (Daemon panel) as a follow-up slice. **No Python changed** for phases
2+3+4 — the whole surface rides the existing `/v1/admin/accounts*` and
`/v1/admin/daemon*` JSON APIs (all bearer-capable via `require_admin()`; CSRF
is skipped for bearer, see `serve/admin/csrf.py::check_csrf`).

- **`is_admin` on the wire.** Rust `WhoamiResponse` carries it with
  `#[serde(default)]`, so a `serve` predating #203 still logs in (falls back
  to `false`) instead of failing to decode. The auth store's `logged_in`
  snapshot exposes `isAdmin`; MainView renders the Admin button from it.
  `screens/AdminView.svelte` is the tabbed overlay (Accounts / Daemon /
  Users / Imports); Accounts and Daemon are implemented, Users and Imports
  are placeholders.
- **Daemon panel (phase 4).** `components/admin/DaemonPanel.svelte` fetches the
  fused `GET /v1/admin/daemon` view (process state + `daemon_heartbeats` +
  recent log) and self-refreshes every `POLL_INTERVAL_MS = 2000` (mirrors the
  web panel's `DAEMON_PANEL_POLL_SECONDS = 2`); the interval is cleared in
  `onDestroy`. Rust proxies live in
  [gui/src-tauri/src/commands/admin/daemon.rs](gui/src-tauri/src/commands/admin/daemon.rs)
  (`daemon_tests.rs` split out) — `get_admin_daemon` (GET), `lifecycle_admin_daemon`
  (POST `/daemon/{start,stop,restart}`, decodes the **202** transitional status),
  `reload_admin_daemon` + `restart_account_sync` (POST, decode `{command_id}`).
  TS wrapper [gui/src/lib/api/admin_daemon.ts](gui/src/lib/api/admin_daemon.ts).
  **Staleness is the server's per-heartbeat `stale` flag alone — never a client
  clock** (matches the web panel + #148); stale rows render red. **Lifecycle
  (start/stop/restart) buttons are disabled when
  `supervise_daemon_externally`** (the launchd deployment), while reload +
  per-account restart-sync stay enabled — those are DB-mediated (Plane A) and
  work regardless of who owns the process. A rejected control (busy-guard /
  external-stub **409**, mapped by `isConflict`) surfaces as a visible
  `daemon-action-message`, never an inert button. The per-account restart-sync
  dedup (idle+poll workers → one button) is the pure, unit-tested
  [gui/src/lib/daemon_view.ts](gui/src/lib/daemon_view.ts)`::restartSyncAccountIds`.
  **CI-trap note:** any admin panel that fetches on mount MUST be stubbed in
  `AdminView.test.ts` **and** `MainView.test.ts` (both mount the overlay) or an
  unhandled promise rejection leaks while vitest still reports "passed" (the
  bug PR #205 caught post-push).
- **Rust proxies** live in
  [gui/src-tauri/src/commands/admin/accounts.rs](gui/src-tauri/src/commands/admin/accounts.rs)
  (tests split into `accounts_tests.rs` via `#[cfg(test)] #[path = …]` to keep
  the module under the size guideline). Each endpoint has a mockito-testable
  `fetch_*`/`post_*` helper + a keyring wrapper + a thin `#[tauri::command]`,
  mirroring `commands::auth_change_password`. `http/client.rs` gained
  `http_patch_json` + `http_delete`.
- **The PATCH body MUST omit unset fields.** `AdminAccountPatch` marks every
  field `#[serde(skip_serializing_if = "Option::is_none")]`. This is
  load-bearing, not style: `api.admin.accounts.update_account` writes *every
  key present* in `fields`, so a serialized `"imap_host": null` **blanks the
  column**. Pinned by
  `patch_update_omits_unset_fields_entirely`. `AccountForm` mirrors this on
  the TS side — it diffs against the loaded row and sends only changed keys,
  which is why a cleared IMAP port cannot be sent. For the same reason
  **`auth_method` is locked on edit** (the selector is `disabled`): every
  transition dead-ends under omit-unset — `→ oauth2` needs an `oauth_provider`
  the web consent flow supplies, and `→ archive` needs `imap_host`/`imap_port`
  nulled, which omit-unset can't express. Changing an account's auth method
  means recreating it. A non-numeric port is rejected inline (not silently
  dropped). Folder-filter editing is not yet in the form (issue #206).
- **Pure modules** (project convention — logic out of components):
  `lib/admin_error.ts` (`httpStatusOf`/`isConflict`/`isForbidden`, a
  depth-bounded walk of the nested `{kind, detail}` Rust error shape, so the
  UI can *act* on a status instead of string-matching `formatError`) and
  `lib/admin_auth_method.ts` (`hasImapEndpoint`/`usesStoredPassword`).
  Routing the auth-method comparisons through functions also stops TS from
  narrowing a local `$state` to its initialiser's literal type, which made
  `authMethod !== "archive"` look unreachable to `svelte-check`.
- **Deliberately absent — do not "finish" without backend work first:**
  Gmail **Connect**. `POST /v1/admin/accounts/{id}/oauth/start` lives in
  `oauth_router.py`, which #203 did *not* swap to `require_admin()`, so it is
  still cookie-only and a bearer client cannot start the flow. The design's
  completion check ("poll secret status until the refresh token appears")
  also has no backing field — `_account_dict` exposes no secret status and no
  `/v1/admin` endpoint reports one. Both are backend gaps. `clear_secret`
  likewise has a service function but no JSON route.
- **Pre-existing, unrelated:** `cargo clippy --all-targets -- -D warnings`
  fails on `gui/src-tauri/src/commands/search.rs:189` (`approx_constant`, a
  `3.14` dummy `took_ms` in a test). It predates this work. CI *does* gate
  clippy (`gui-ci.yml` runs `cargo clippy --locked -- -D warnings`) but
  **without `--all-targets`**, so `#[cfg(test)]` modules are never linted —
  hence a green `main`. Use the bare CI invocation when checking locally.

## Conventions

- **No comments unless the WHY is non-obvious.** Don't restate the SQL or the
  Python.
- **Don't write `.eml` fixtures to disk** — `tests/_eml.py` builds messages
  programmatically with `email.message.EmailMessage`. Same goes for any future
  test fixture: generate, don't check in.
- **DB tests** TRUNCATE before each test (see the `db_conn` / `pool` fixtures).
  Tests must work against the live test DB; never `DROP TABLE`.
- **No `cur.fetchone()[0]` without `assert row is not None` first** — mypy is
  enabled (`[tool.mypy]` in `pyproject.toml`) and will flag it. Note that mypy
  only catches this when the `conn` parameter is annotated
  (`conn: psycopg.Connection`); on an unannotated `conn` the cursor is `Any`
  and the violation passes silently, so annotate every new DB helper.
- New SQL goes in a new numbered migration file. **Never edit a migration
  that has been applied anywhere** — add the next-numbered file instead.
  Latest is `0032_channel_subscriptions.sql`; next free slot `0033_*.sql`.
  (2B.4 and 2B.5 added no migration — the supervisor, routes, CLI, and admin
  panel are stateless and reuse `0023_daemon_heartbeats.sql` +
  `0024_daemon_commands.sql`.)

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
