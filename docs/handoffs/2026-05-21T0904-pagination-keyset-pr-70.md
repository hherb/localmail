# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-21 (post-session).** PR **#70**
> (`fix(search): unbounded sort=date, coalesced wire date, reranker off
> by default`) **open against `main`** on branch `feat/pagination`. 17
> commits, ~+2620 / -147 lines. Both CI checks green
> (`svelte-check + vitest`, `cargo test + clippy`). Full pytest suite
> **731 passed** (was 682 at session start; +49 from new browse / cursor
> / sort=date keyset tests). GUI vitest **271 passed**. mypy clean on
> touched files; 4 pre-existing `parser.py` errors carry forward.
> Awaiting review + merge.
>
> Two follow-up issues filed during the in-PR review pass:
> **#71** (Searcher accessor refactor — drop private-attr reads) and
> **#72** (`EXPLAIN ANALYZE` confirming `messages_recent_idx` is used
> under the per-user ACL filter). Both scoped as separate PRs.
>
> Prior session's PR **#69** (Postgres-backed login rate limiter,
> closes #7) **merged** to `main` on 2026-05-20 — no leftover work
> there. CLAUDE.md still describes the limiter accurately.
>
> Branch `feat/pagination` lives locally + on origin; keep until
> PR #70 merges. Working tree clean (only `.claude/settings.local.json`
> untracked — local-only).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

The two production use cases drive every design decision:

1. Personal searchable mail archive (human user).
2. Backing service for AI agents that may hammer it with high
   concurrency — `uvicorn --workers N` is a near-term reality.

This session was scoped against use case (1): the GUI's "Load more"
on a date-sorted query topped out at ~20 results, dates looked out of
order on forwarded mail, and the cursor path timed out past the
cache. None of that was acceptable on a real archive.

## What we shipped this session

### PR #70 — Pagination + sort=date keyset + reranker default off

Branch: `feat/pagination` (head `5e78ad4`).

#### Backend — `/v1/messages` keyset browse + `/v1/search` cursor

| SHA | What |
|---|---|
| `118504c` | `feat(config)`: add `candidates_per_arm_max` (default 800) — ceiling for transparent `grow_pool` growth on the cursor path. |
| `1b5c7ac` | `feat(api)`: opaque browse-cursor codec in `localmail.api.browse_cursor` — URL-safe base64 of `"d|<iso-ts>|<id>"` or `"n|<id>"`. Pure module, transport-free (MCP can reuse). |
| `6ed89d1` | `feat(api)`: `list_messages` service — keyset pagination on `COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC` (matches `messages_recent_idx`). ACL applied at the SQL boundary; empty grants short-circuit. |
| `de1b7d1` | `test(api/browse)`: NULL-date tail, ACL intersection, folder filter. |
| `b95f0a8` | `feat(serve)`: `GET /v1/messages` route. Cursor validated **before** the ACL check so malformed tokens always 400 even with no grants. |
| `614df1c` | `feat(api)`: `SearchCursorExpired` 409 problem type — surfaces page-cache miss (TTL / LRU / cross-user replay) distinctly from a generic 500. |
| `1c67683` | `feat(api)`: search cursor codec (`"<token>:<page>"`). 1-based page; malformed → `ValidationFailed` → 400. |
| `870b6c6` | `feat(serve/search)`: cursor wired through `/v1/search`. `run_search` branches on `cursor`: None → `search()`; otherwise `continue_page()` + transparent `grow_pool()` up to `candidates_per_arm_max`. |

#### GUI — Tauri command + TS wrapper + stores + MessageList

| SHA | What |
|---|---|
| `c49032c` | `feat(gui/tauri)`: `list_messages_cmd` for GET `/v1/messages` — forwards repeatable `account_id`/`folder_id`, `limit`, `cursor`. |
| `e15ce45` | `feat(gui)`: TS wrapper + `ListMessagesResponse` type; re-exported from `tauri.ts`. |
| `ec90882` | `feat(gui/mail)`: store now fetches `/v1/messages` for initial load (was `/v1/changes` initial-fetch). `loadMoreMessages` appends + advances the cursor; concurrent IntersectionObserver firings coalesce on a shared in-flight promise. `setSelection` refetches scoped to the active filter. |
| `430d7a5` | `feat(gui/mail)`: `pollOnce` no longer auto-prepends. New polled messages land in `pendingNewMessages` (dedup against `messages` and `pending`); `mergePendingNewMessages` is the user-action commit. |
| `36fc3ff` | `feat(gui/search)`: cursor + `loadMore`. On 409 `search-cursor-expired` the store re-runs the query without a cursor, drops already-held rows, and appends the rest. |
| `1509d8a` | `feat(gui)`: bottom sentinel (IntersectionObserver, `rootMargin: 200px`) drives `loadMore` on the active store; visible "Load more" button shares the handler. "N new messages" banner shows pending poll arrivals. |
| `e368318` | `fix(gui)`: `isSearchCursorExpired` now handles Tauri's `{kind, detail}` reject shape — the old predicate fell back to `String(err) = "[object Object]"`, so transparent 409 recovery never fired in production. |

#### Sort=date regression fix + tests + the keyset path

| SHA | What |
|---|---|
| `10b2145` | `fix(search)`: three regressions in one. (1) `sort=date` + non-empty `free_text` now takes a **lexical keyset** path (`Searcher._lexical_date_search` — `WHERE fts_v2 @@ plainto_tsquery('simple', q) ORDER BY COALESCE(internal_date, date_sent) DESC, id DESC` with keyset pagination, no pool cap, unbounded scroll). New `KeysetCursor` on `SearchPage.next_keyset`; wire cursor uses `"K\|<base64>"` prefix to distinguish from `"token:page"`. Route dispatches by prefix. (2) Wire `date` is now `COALESCE(internal_date, date_sent)` everywhere it was `date_sent` (search, browse, changes) — matches the SQL sort key. `SearchResult.internal_date` plumbed through `_hydrate` / `_build_results` / `_list_recent_messages`. (3) `rerank_pool_size` 20→100 so a `sort=rank` first page fills `limit=50` and one follow-up loads from cache; `reranker_enabled` True→**False** by default (CPU-bound rerank overruns timeouts when `grow_pool` doubles the pool). |
| `5e78ad4` | `test(search)`: cover the dated→NULL keyset boundary (end-to-end DB walk), the cap-hit branch of `_continue_or_grow` (must not loop on `grow_pool`), and the `K\|…` cursor dispatch path. Drop redundant `last[0]` read; document the OFF-by-default reranker in `config.example.toml`. |

### Issues filed this session

- **#71** — `refactor(search): expose Searcher pool metadata via accessor`.
  `serve/routes/search.py` currently reaches into Searcher private
  attributes; a small `Searcher.pool_meta(token)` accessor is cleaner.
- **#72** — `perf(browse): verify messages_recent_idx is used under
  account_id = ANY(...) ACL filter`. The expression index is the
  load-bearing piece for keyset browse; `EXPLAIN ANALYZE` under a
  realistic ACL is worth checking before a large archive ships.

### Docs updates this session

- **README.md**: tuning section now mentions `candidates_per_arm_max`,
  the bumped `rerank_pool_size` default (100), and the
  `reranker_enabled = false` default with the reason. A new "Browse &
  search pagination" subsection inside the GUI server section
  documents `/v1/messages`, both `/v1/search` cursor flavours, the 409
  transparent-recovery path, and the wire `date = COALESCE(...)`
  invariant.
- **CLAUDE.md**: the canonical-ordering paragraph now lists
  `_lexical_date_search` and `list_messages` as users of
  `messages_recent_idx`, and pins the wire-`date` invariant with the
  three guard tests. A new bullet in the GUI server section captures
  the cursor-flavour distinction, the page-cache eviction → 409
  contract, the `reranker_enabled` default-flip rationale, and the
  #71 / #72 follow-up scope.

### Test deltas

```
backend pytest:    682 → 731  (+49)
gui vitest:        271 unchanged
mypy:              4 pre-existing parser.py errors (unchanged)
```

## What's next

### 1. Merge PR #70

CI is green and the PR text already describes the change well. After
review and merge:

```bash
git checkout main
git pull
git branch -d feat/pagination
git push origin :feat/pagination
```

The PR's "Test plan" has one unchecked manual smoke item — search
`"e-ticket"` with `sort=date` and walk "Load more" through every
match. Run it on the live archive before approving.

### 2. Pick the next piece

In order of recommendation:

- **`auth.trust_proxy_headers`** (no issue yet — file one).
  Carried forward from the PR #69 handoff. The per-IP login cap is
  effectively global behind a reverse proxy because
  `request.client.host` is the socket peer. A small config knob plus
  `_resolve_client_ip(request, cfg)` honouring the leftmost public
  IP in X-Forwarded-For (only when trust is enabled) closes the
  gotcha documented in CLAUDE.md + README.md. Small, well-scoped,
  same shape as recent config additions.
- **#71** Searcher accessor refactor — small, mechanical, removes a
  private-attribute boundary violation in the search route.
- **#72** `EXPLAIN ANALYZE` of `messages_recent_idx` under the
  `account_id = ANY(...)` ACL filter. No code change expected; if
  the index isn't used, file a covering index instead.
- **`grow_pool` deep-pagination duplicates on `sort=rank`** — when
  the cache exhausts past pool size 100, `grow_pool` returns page 1
  of the enlarged pool, surfacing already-seen top hits. The PR #70
  body explicitly defers this. `sort=date` now covers "show me
  everything" so this is lower priority; revisit if rank-paginated
  duplicates become user-visible.
- **#38** `/v1/changes` semantics decision — now that the GUI's
  initial load goes through `/v1/messages`, `/v1/changes` is only
  the delta-fetch path. Worth resolving while the change is fresh.
- **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
  `main` still need triage. Independent of this session.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`).
- **#4 / #2 / #5** Search-perf follow-ups (model paths, CONCURRENT
  GIN build, batch INSERT for chunking).
- **#25** `websockets.legacy` DeprecationWarning — still blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on real ops data.

## Open decisions & risks

1. **Two cursor flavours on `/v1/search` (`"<token>:<page>"` vs
   `"K|<base64>"`).** The route dispatches on prefix. Any new client
   must treat the cursor as opaque — never parse or compare. Tests
   in `test_serve_search_route.py` lock the contract.

2. **`reranker_enabled` default flip is a behaviour change for
   existing users.** Upgrade path: the OFF default means rank-only
   ordering on `sort=rank` without an explicit `[search]
   reranker_enabled = true`. CLAUDE.md + README.md document both the
   reason and the GPU opt-in. If a user reports "results got worse",
   point them at this knob first.

3. **`grow_pool` on `sort=rank` past cache.** Returns page 1 of the
   enlarged pool, surfacing duplicates. Filed as inline TODO in the
   PR body; not blocking for this PR because `sort=date` now serves
   the "show me everything" intent unbounded.

4. **Page-cache eviction is per-process.** `serve --workers N`
   workers don't share the page cache, so a cursor minted on worker
   A may 409 on worker B. The GUI's transparent re-run + skip path
   handles this gracefully, but the "feels like the same query just
   refetched 200 rows" cost is real on large pools. Sharing the
   cache via Redis is a Phase-5 concern, not this PR.

5. **#71 (Searcher accessor) and #72 (ACL-filtered EXPLAIN ANALYZE)**
   are deliberately deferred — they were caught in the in-PR review
   pass, scoped small, and don't change behaviour. Picking them up
   immediately keeps the PR-70 surface area tight.

6. **`_lexical_date_search` duplicates ACL plumbing.** Cleanly
   factored helpers (`_apply_acl_filter`) already exist for other
   arms; this new path re-derived its own. #71 covers it. Low-risk
   to live with for one merge cycle.

7. **Carried forward from prior sessions (still load-bearing):**
   - Postgres-backed login rate limiter (#7) merged in PR #69. The
     `auth.trust_proxy_headers` gap is still open.
   - The MIME clamp list (#32) is small on purpose — only
     actively-script-executable types.
   - `parse_int_id` (#33) accepts leading zeros (`"01"` → 1).
   - `rrf_k=60` is the centre of a flat plateau (#35).
   - `websockets.legacy` DeprecationWarning (#25) — uvicorn blocker.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin

# If PR #70 is still open:
git checkout feat/pagination
gh pr view 70                              # check CI + review state

# After PR #70 is merged:
git checkout main
git pull
git branch -d feat/pagination
git push origin :feat/pagination

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q              # expect 731 passed
unset VIRTUAL_ENV && uv run mypy src/localmail     # 4 pre-existing parser.py errors
cd gui && npm test --silent -- --run               # expect 271 passed

# Pick next piece:
gh issue list --state open --limit 40
# Recommended: file auth.trust_proxy_headers issue, then ship it as
# the natural follow-up to PR #69's documented proxy gotcha.
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  picks the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0019 are additive.** Re-running `init-db` on an
  older archive is safe (idempotent), but back up first if the archive
  is non-trivial. Next migration would be `0020_*.sql`.
- **Wire `date` = `COALESCE(internal_date, date_sent)`** on every
  paginated list endpoint. New endpoints that emit `date_sent`
  directly will fail the guard tests in `test_serve_browse_route.py`
  / `test_serve_search_route.py` / `test_serve_changes_route.py`.
- **Two cursor flavours on `/v1/search`** — `"<token>:<page>"` (pool)
  and `"K|<base64>"` (keyset, `sort=date` + non-empty query).
  Route dispatches by prefix; never parse cursors client-side.
- **Page-cache miss → HTTP 409 `/problems/search-cursor-expired`**,
  never 500. The GUI's transparent re-run path expects this exact
  problem type — don't generalise it.
- **`reranker_enabled` default = False.** CPU-bound cross-encoder
  rerank fanout overruns timeouts when `grow_pool` doubles the
  pool (50 → 100 → … → 800). Flip in `config.toml` only on GPU
  hosts.
- **Login rate limiter (#7, PR #69)** still load-bearing:
  - Caps live in `LocalmailConfig.auth`.
  - `_record_login_attempt` + `_maybe_sweep` commit eagerly. New
    writes added to `login()` between these and a subsequent
    `raise` must wrap their own SAVEPOINT if they must NOT survive
    an outer rollback.
  - `auth.trust_proxy_headers` doesn't exist yet — bump
    `login_global_max` if running behind a reverse proxy.
- **TLS for `localmail serve`** — `--bind 0.0.0.0` requires
  `--tls-cert` + `--tls-key`. `--no-tls` is only honoured on
  `127.0.0.1`. Use `localmail rotate-tls` to generate a self-signed
  cert.
- **First-time `body_lang` install** — run `localmail lang-backfill`
  (or `embed-backfill`, which drains both queues) once after upgrade
  so `lang:` queries return rows.
- **ACL upgrade**: post-0016, new API users have **no grants**. Run
  `localmail grant-account USERNAME ACCOUNT_NAME` once per pair.
- **Probe-then-condition boundary** (#62): for any new
  conditional-GET endpoint, the order is
  **ACL+probe → precondition → expensive IO**.
- **Streaming WARNING contract** (#58): any new streaming endpoint
  that advertises a `Content-Length` MUST also count bytes yielded
  and call `_log_truncation()` when the source runs short.
- **ID-typing boundary** (#33): path and query parameters bearing
  entity IDs are typed `str` on the route handler signature, and
  `localmail.api.ids.parse_int_id(value, field="…")` is the ONLY
  way to cast to int.

## File map (as of branch HEAD `5e78ad4`)

```
src/localmail/
  api/                               # transport-free service library
    browse_cursor.py                 # NEW (#70): opaque base64 codec
    search_cursor.py                 # NEW (#70): "<token>:<page>"
    search.py                        # run_search dispatches K|… vs token:page
    messages.py                      # NEW: list_messages keyset service
    errors.py                        # SearchCursorExpired added
    auth.py acl.py attachments.py
    conditional.py ids.py range_requests.py sanitize.py
  config.py                          # candidates_per_arm_max (#70)
                                     # rerank_pool_size 20→100
                                     # reranker_enabled True→False
  search/
    searcher.py                      # _lexical_date_search (#70)
                                     # SearchResult.internal_date
                                     # SearchPage.next_keyset
    arms.py chunking.py embeddings.py
    embed_worker.py extract_worker.py extractor.py
    lang_detect.py page_cache.py query.py reranker.py
  serve/
    routes/
      messages.py                    # GET /v1/messages keyset browse
      search.py                      # cursor wiring, prefix dispatch
      changes.py                     # wire date = COALESCE(...)
      accounts.py attachments.py auth.py version.py
    app.py middleware.py
  cli.py daemon.py worker.py ...
migrations/                          # 0001 … 0019_api_login_attempts.sql
tests/                               # 731 passing
  test_api_browse.py                 # NEW: list_messages (#70)
  test_api_browse_cursor.py          # NEW: codec round-trips (#70)
  test_api_search_cursor.py          # NEW: token:page codec (#70)
  test_api_search_cursor_error.py    # NEW: SearchCursorExpired (#70)
  test_api_search_pagination.py      # NEW: continue_page / grow_pool (#70)
  test_searcher.py                   # _lexical_date_search + keyset (#70)
  test_serve_browse_route.py         # NEW: /v1/messages route (#70)
  test_serve_search_route.py         # cursor flow + K|… dispatch (#70)
  test_serve_changes_route.py        # wire date invariant (#70)
  conftest.py
docs/handoffs/
  2026-05-21T0904-pagination-keyset-pr-70.md  # this session's snapshot
NEXT_SESSION.md                      # this file (post-session)
gui/                                 # 271 GUI tests passing
  src-tauri/.../list_messages_cmd
  src/lib/api/tauri.ts
  src/lib/stores/mail.ts             # banner + loadMoreMessages (#70)
  src/lib/stores/search.ts           # 409 transparent recovery (#70)
  src/lib/views/MessageList.svelte   # IntersectionObserver sentinel (#70)
```

End of pagination + sort=date keyset + reranker default-off session.
PR #70 open against `main` (`5e78ad4`). Branch `feat/pagination` alive
on local + remote until merge. Next: merge #70, then ship the
`auth.trust_proxy_headers` follow-up (carried over from PR #69).
