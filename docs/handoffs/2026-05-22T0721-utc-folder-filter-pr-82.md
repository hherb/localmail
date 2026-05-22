# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-22 (post-session).** PR **#82**
> (`test(browse): folder-filter plan coverage in harness + unit
> tests (closes #78)`) **open against `main`** on branch
> `test/78-folder-filter-coverage` (head `0d058ab`). One commit,
> +528 / −23, three files. Full pytest suite **786 passed**
> (was 781; +3 new tests in `test_api_browse_plan.py` plus +2
> unexplained baseline drift). mypy clean on touched files; 4
> pre-existing `parser.py` errors carry forward unchanged.
> Awaiting review + merge.
>
> Prior session's PR **#81** (`refactor(browse): canonical SQL
> emitter shared by tests/harness (closes #77)`) **merged** to
> `main` on 2026-05-22 as `e28dc2e`. No leftover production
> work; branch `chore/77-canonicalise-browse-sql` already
> pruned locally.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session closed #78 — the folder-filter coverage gap. The
`messages_recent_idx` eligibility tests (PR #76) and the EXPLAIN
harness covered only the folderless initial-load path. The
`folder_ids` branch of `list_messages` — which the GUI will hit once
folder filtering ships — was unmeasured. Now it isn't: three new
eligibility tests pin the SQL-shape regression, the harness's
`--folder-filter` flag exercises selective and broad folders at any
scale, and the verdict logic splits folderless from folder-filter
recommendations so a covering-index recommendation stays folderless-
only (option 2 firing on a narrow folder is the right planner choice).

## What we shipped this session

### PR #82 — Folder-filter plan coverage (closes #78)

Branch: `test/78-folder-filter-coverage` (head `0d058ab`).
Single commit. No production code change.

#### Implementation

| SHA | What |
|---|---|
| `0d058ab` | `test(browse)`: extend `tests/acceptance/run_browse_explain.py` with `--folder-filter` flag that seeds two mailboxes per account (`selective` ~5%, `broad` ~50%) and appends 4 probes (selective+heavy, broad+heavy, broad+heavy mid-keyset, broad-across-accounts). Verdict logic splits folderless from folder-filter results. New `_seed_folder_filter_mailboxes`, `FolderMailboxes` dataclass, `_build_folder_filter_probes`. Three new unit tests in `tests/test_api_browse_plan.py`: `test_messages_recent_idx_is_eligible_for_{narrow,broad,multi}_folder_filter` — same SAVEPOINT scaffolding as the folderless eligibility tests, exercises `compose_browse_sql(folder_filter=True)`. CLAUDE.md gains a "Folder-filter planner choice (#78, resolved)" paragraph alongside the #72 / #75 / #77 findings. |

#### Acceptance — issue #78 criteria

| | criterion | satisfied by |
|---|---|---|
| ✅ | Extend harness with a folder-filter dimension (selective + ~50% folder) | `--folder-filter` flag; 4 new probes appended; verdict split into folderless + folder-filter |
| ✅ | Add eligibility test for the folder-filter SQL shape | `test_api_browse_plan.py` gains `narrow`, `broad`, `multi` variants — all green; pin `Index Scan using messages_recent_idx on messages` under JOIN |
| ✅ | Document planner choice in CLAUDE.md alongside #72 | new "Folder-filter planner choice (#78, resolved)" paragraph documents selectivity-dependent plan, harness verdict split, and the unavoidable DISTINCT-induced sort |

#### Operational verdict (production scale)

200k rows × 5 accounts × skewed, `--folder-filter`:

```
ACL=1 heavy | initial | folder=selective          index-walk (option 1)  1.31ms  25837 hits
ACL=1 heavy | initial | folder=broad              index-walk (option 1)  0.20ms   3519 hits
ACL=1 heavy | mid | folder=broad                  index-walk (option 1)  0.21ms   3681 hits
ACL=all | initial | folder=broad-across-accounts  index-walk (option 1)  0.20ms   3381 hits
```

Every folder-filter probe picks `Index Scan using messages_recent_idx`.
The selective folder probe is heaviest (admits ~5%, so LIMIT walks more
rows before short-circuiting) but still 1.3ms — acceptable.

At small scale (5k rows × 3 accounts × balanced), the planner correctly
picks bitmap heap scan / seq scan for the narrow probes. That's
*right* — at narrow selectivity the index walk is more expensive than
starting from `message_labels`. The verdict treats folder-filter
results as informational so a covering-index recommendation doesn't
fire spuriously.

### Test deltas

```
backend pytest:    781 → 786  (+3 new folder-filter eligibility tests;
                               +2 unexplained baseline drift — likely
                               uncounted from prior PRs, not caused
                               by #78)
mypy:              4 pre-existing parser.py errors (unchanged)
```

### Docs updates this session

- **CLAUDE.md** — added a "Folder-filter planner choice (#78, resolved)"
  paragraph under the `messages_recent_idx` section, documenting the
  selectivity-dependent planner choice, the harness verdict split, and
  the inherent DISTINCT-induced post-join Sort.
- **README.md** — unchanged (no user-visible change).
- **ROADMAP.md** — file does not exist in this repo; no update needed.

## What's next

### 1. Merge PR #82

Test/docs-only. After merge:

```bash
git checkout main
git pull
git branch -d test/78-folder-filter-coverage
# origin branch auto-deleted on merge.
```

### 2. Pick the next piece

In rough order of recommendation:

- **#79** — `run_browse_explain: _pick_mid_cursor scales linearly with
  --total-rows (OFFSET COUNT/2)`. Filed alongside #78 during the #72
  session. The harness's OFFSET-based cursor picker becomes slow on
  huge archives. Swap for a percentile pick via TABLESAMPLE or a
  pre-computed median. Also fix the broken `_scan_actual_rows` while
  you're in the file — the harness still reports `actual_rows=0`
  because the parser searches for `actual rows=` but EXPLAIN ANALYZE
  emits `(actual time=X..Y rows=Z loops=W)`. Not load-bearing for the
  verdict logic, but noisy.
- **Follow-up filed during #78 work**: the `SELECT DISTINCT` shape on
  the folder-filter SQL forces a post-join Sort+Unique pass over every
  projected column. This is correct (the JOIN to `message_labels` can
  multiply rows when `folder_ids` admits more than one mailbox), but
  the search arms in `src/localmail/search/arms.py` use the cleaner
  `EXISTS (SELECT 1 FROM message_labels …)` semi-join shape — no
  DISTINCT, no post-join sort. Worth measuring whether browsing should
  switch to the same shape; would be a meaningful perf win on broad
  folder filtering at scale. **Not filed yet — file as issue if
  someone takes #79.** Acceptance criteria: a benchmark showing how
  much the DISTINCT-induced Sort costs at 200k rows + broad folder,
  and whether `EXISTS` is materially faster.
- **PR-73 follow-up cleanup** — bundle the 5 minor polish items
  filed in the PR #73 handoff into one small PR: move inline
  `from ipaddress import IPv4Network` imports in
  `tests/test_config.py:243,250` to module top; drop the
  defensive `object.__setattr__` form in
  `src/localmail/config.py:100-109`; canonicalise the
  `TrustedProxies` alias (currently duplicated in
  `client_ip.py` and `config.py`); optionally add a TOML
  round-trip test for `auth.trusted_proxies` keys.
- **`grow_pool` deep-pagination duplicates on `sort=rank`** —
  carried from PR #70 handoff. When the cache exhausts past
  pool size 100, `grow_pool` returns page 1 of the enlarged
  pool, surfacing already-seen top hits. `sort=date` covers
  "show me everything" so this is lower priority; revisit if
  rank-paginated duplicates become user-visible.
- **#38** `/v1/changes` semantics decision — now that the GUI's
  initial load goes through `/v1/messages` (PR #70) and the
  cursor pagination is range-bounded (PR #80), `/v1/changes`
  is only the delta-fetch path. Worth resolving while the
  change is fresh.
- **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
  `main` still need triage. Independent of this session. The
  PR-push warning surfaced it again.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`).
- **#4 / #2 / #5** Search-perf follow-ups (model paths, CONCURRENT
  GIN build, batch INSERT for chunking).
- **#25** `websockets.legacy` DeprecationWarning — still blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on real ops data.

## Open decisions & risks

1. **Folder-filter SQL still uses `SELECT DISTINCT m.id, ...`** —
   inherent post-join Sort+Unique pass over every projected column.
   At 200k rows × broad folder, this is the dominant cost on the
   folder-filter path. The search arms in `arms.py` use the
   `EXISTS (SELECT 1 FROM message_labels …)` semi-join shape — no
   DISTINCT, no post-join sort. The browse path could plausibly
   switch to the same shape for the folder-filter case. Not filed
   as a follow-up issue yet; file it if/when someone picks #79 or
   measures user-visible browse latency on broad folder filtering.
   The eligibility tests document this as "expected" — they DO NOT
   assert "no full sort" for the folder-filter case (as they do for
   the folderless case) because the DISTINCT-induced sort is inherent
   to the existing SQL shape.

2. **The harness verdict split (folderless vs folder-filter) is
   load-bearing.** Don't collapse them back into one verdict. The
   "option 2 fires → covering index needed" recommendation is
   folderless-only — option 2 firing on a narrow folder is the right
   planner choice (bitmap-on-`mailbox_id` + nested loop into messages
   by PK), not a problem to flag. A future schema change that broke
   the folderless invariant would still be caught by the folderless
   verdict.

3. **Test baseline drift (+5 vs prior session).** I added 3 tests but
   pytest reports +5 over the 781 baseline. The +2 unexplained delta
   was also present at session start (NEXT_SESSION.md from PR #81
   noted "776 → 781 likely uncounted from prior PRs"). Not caused
   by anything in this session. Worth tracking down if it grows.

4. **The harness's `_scan_actual_rows` is still broken** (pre-existing,
   carried from prior sessions). It searches for `"actual rows="` but
   EXPLAIN ANALYZE emits `(actual time=X..Y rows=Z loops=W)` — no
   `actual rows=` substring. `actual_rows` is always 0 in the harness
   output. Not load-bearing for the #75 / #78 verification
   (`rows_removed_by_filter` is the key metric and is parsed
   correctly). Worth fixing as part of #79 if someone touches the
   harness.

5. **Pyright (the VSCode language server) complains about
   `cur.execute(sql, ...)` where `sql` is a composed `str`.** This
   is psycopg's `LiteralString` typing — Pyright is stricter than
   mypy on this. mypy is the authoritative type check (per
   `pyproject.toml`) and is clean on touched files. The IDE
   diagnostic is a non-blocker.

6. **Carried forward from prior sessions (still load-bearing):**
   - **PR #81** (`#77` — canonical browse SQL emitter) — merged
     this session start. `BROWSE_ROW_SQL_TEMPLATE` +
     `compose_browse_sql` + `build_where` in
     `src/localmail/api/browse.py` are the only authoritative SQL
     emitter for the browse path. Tests + harness compose via the
     production primitives — do NOT re-introduce inline SQL.
   - **PR #80** (`#75` — row-comparison keyset + NULL-tail top-up)
     — merged 2026-05-22 as `de06afe`. Mid-keyset browse pagination
     is range-bounded; do NOT rewrite the dated cursor predicate
     to the equivalent OR form.
   - **PR #76** (`messages_recent_idx` planner choice verified)
     — ACL-filtered browse uses the index walk at production
     scale across all distribution shapes. Pinned by
     `tests/test_api_browse_plan.py`.
   - **PR #74** (`Searcher.get_pool_metadata` + `Searcher.config`)
     — the api/ layer (and any future MCP layer) must use the
     public accessors, not `searcher._cache` / `searcher._cfg`.
   - `auth.trusted_proxies` (#73) — opt-in CIDR list governs both
     admission and peeling. Empty default = unchanged behaviour.
   - Postgres-backed login rate limiter (#7, PR #69) — caps live in
     `LocalmailConfig.auth`.
   - PR #70 (`sort=date` keyset, reranker off-by-default) —
     `reranker_enabled = false` default stands.
   - The MIME clamp list (#32) is small on purpose — only
     actively-script-executable types.
   - `parse_int_id` (#33) accepts leading zeros (`"01"` → 1).
   - `rrf_k=60` is the centre of a flat plateau (#35).
   - `websockets.legacy` DeprecationWarning (#25) — uvicorn blocker.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin

# If PR #82 is still open:
git checkout test/78-folder-filter-coverage
gh pr view 82                              # check CI + review state

# After PR #82 is merged:
git checkout main
git pull
git branch -d test/78-folder-filter-coverage

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                          # expect 786 passed
unset VIRTUAL_ENV && uv run mypy src/localmail     # 4 pre-existing parser.py errors

# Re-run the acceptance harness with folder-filter:
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \
    --total-rows 200000 --accounts 5 --distribution skewed --folder-filter
# Expect: every probe (folderless + folder-filter) → index-walk (option 1)

# Or at small scale to see option 2 fire on narrow selectivity (correct):
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \
    --total-rows 5000 --accounts 3 --distribution balanced --folder-filter
# Expect: VERDICT (folder-filter) reports mixed option 1 / option 2;
#         VERDICT (folderless) reports option 1 only.

# Pick next piece:
gh issue list --state open --limit 40
# Recommended: #79 (run_browse_explain perf cleanup) — touches the
# same harness file, includes fixing _scan_actual_rows.
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
  paginated list endpoint (PR #70). Guard tests in
  `test_serve_browse_route.py` / `test_serve_search_route.py` /
  `test_serve_changes_route.py` enforce.
- **Two cursor flavours on `/v1/search`** — `"<token>:<page>"` (pool)
  and `"K|<base64>"` (keyset, `sort=date` + non-empty query).
  Route dispatches by prefix; never parse cursors client-side.
- **Page-cache miss → HTTP 409 `/problems/search-cursor-expired`**,
  never 500. The GUI's transparent re-run path expects this exact
  problem type.
- **`reranker_enabled` default = False.** CPU-bound cross-encoder
  rerank fanout overruns timeouts when `grow_pool` doubles the
  pool. Flip in `config.toml` only on GPU hosts.
- **`auth.trusted_proxies`** must contain the proxy's CIDR for the
  per-IP login cap to read the real client. Empty default = unchanged
  behaviour. Do NOT also set `uvicorn --forwarded-allow-ips` —
  collapses the admission check.
- **`trusted_proxies` validator fails LOUD at config load** on a bad
  CIDR. `trusted_proxies_max_hops` clamps to `[1, 10]` via pydantic
  Field constraints. Misconfig surfaces at startup, never at
  request time.
- **Login rate limiter (#7, PR #69)** still load-bearing: caps live
  in `LocalmailConfig.auth`. `_record_login_attempt` + `_maybe_sweep`
  commit eagerly.
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
- **`Searcher` public boundaries** (PR #74). The api/ layer (and
  any future MCP layer) must use
  `searcher.get_pool_metadata(token, *, user_id)` and
  `searcher.config` — never reach into `searcher._cache` or
  `searcher._cfg`.
- **`messages_recent_idx` planner choice (#72, PR #76)** — the
  planner uses this index for ACL-filtered browse queries at
  production scale across all distribution shapes. No covering
  index is needed.
- **Dated-cursor predicate MUST use ROW comparison** (#75, PR #80) —
  `ROW(COALESCE(internal_date, date_sent), m.id) < ROW(%s, %s)`.
  Do NOT rewrite to the equivalent OR form (`expr < X OR (expr
  = X AND id < Y)`) — Postgres refuses to decompose mixed-column
  ORs into an index range bound and the predicate degrades to a
  post-walk `Filter:` walking ~`total_rows / 2` tuples per page.
- **NULL-tail top-up is conditional** (#75, PR #80) — only runs
  when `cursor is not None AND cursor.ts is not None AND
  len(rows) < fetch_limit`. Don't collapse the conditional;
  the initial page already naturally returns NULL rows via
  NULLS-LAST.
- **Canonical browse SQL emitter** (#77, PR #81) —
  `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql` + `build_where`
  in `src/localmail/api/browse.py` are the only authoritative SQL
  emitter for the browse path. The eligibility tests in
  `tests/test_api_browse_plan.py` and the EXPLAIN harness in
  `tests/acceptance/run_browse_explain.py` compose via these
  primitives. Do NOT re-introduce inline SQL copies in tests or
  harnesses; that's exactly the drift the refactor killed.
- **`compose_browse_sql` is the allowed way to call `.format()` on
  the template** (#77, PR #81). Direct `BROWSE_ROW_SQL_TEMPLATE.format(...)`
  calls skip the `_FOLDER_JOIN_SQL` invariant. The harness `pre75`
  variant is the one allowed exception (deliberately divergent
  WHERE clause).
- **NEW from this session: folder-filter planner choice** (#78, PR #82) —
  at production scale, every folder-filter probe picks
  `Index Scan using messages_recent_idx` (selectivity-dependent at
  smaller scales). The harness verdict split (folderless vs
  folder-filter) is load-bearing: the covering-index recommendation
  stays folderless-only because option 2 on a narrow folder is the
  right planner choice, not a problem to flag.
- **NEW from this session: folder-filter eligibility assertions don't
  forbid Sort nodes** (#78, PR #82) — the `SELECT DISTINCT` semantics
  on the production browse SQL forces a post-join Sort+Unique pass
  over every projected column when `folder_filter=True`. This is
  inherent to the JOIN+DISTINCT shape, not an index-eligibility
  regression. The folder-filter eligibility tests assert
  `Index Scan using messages_recent_idx on messages` is present
  and `Bitmap Heap Scan on messages` is absent, but DO NOT assert
  "no full sort" — that would fail on the DISTINCT-induced sort.

## File map (as of branch HEAD `0d058ab`)

```
src/localmail/
  api/
    browse.py                          # unchanged (PR #81 canonical
                                       # SQL emitter — used as-is by
                                       # the new folder-filter tests)
    search.py                          # unchanged
    client_ip.py auth.py acl.py
    attachments.py browse_cursor.py conditional.py
    errors.py ids.py messages.py range_requests.py
    sanitize.py search_cursor.py
  config.py                            # unchanged
  search/                              # unchanged
    searcher.py arms.py chunking.py embed_worker.py embeddings.py
    extract_worker.py extractor.py lang_detect.py
    page_cache.py query.py reranker.py
  serve/                               # unchanged
    routes/
      auth.py messages.py search.py changes.py
      accounts.py attachments.py version.py
    app.py middleware.py
  cli.py daemon.py worker.py ...
migrations/                            # 0001 … 0019_api_login_attempts.sql
tests/                                 # 786 passing
  acceptance/
    run_browse_explain.py              # MODIFIED (PR #82):
                                       # --folder-filter flag,
                                       # FolderMailboxes dataclass,
                                       # _seed_folder_filter_mailboxes,
                                       # _build_folder_filter_probes,
                                       # verdict split (folderless +
                                       # folder-filter)
    run_recall_eval.py run_attachment_eval.py run_rrf_k_sweep.py
  test_api_browse_plan.py              # MODIFIED (PR #82): +3 new
                                       # eligibility tests for
                                       # narrow / broad / multi
                                       # folder-filter SQL shape;
                                       # new helpers
                                       # _seed_mailbox_for_plan_test,
                                       # _label_fraction_of_account,
                                       # _explain_folder_filter_recent_idx_only
  test_api_browse.py                   # unchanged from PR #81
  test_api_browse_cursor.py            # unchanged
  test_searcher_pool_metadata.py       # PR #74 (merged)
  test_api_search_pagination.py        # PR #74 (merged)
  test_api_search.py test_api_search_cursor.py
  test_api_search_cursor_error.py test_api_search_lang_dates.py
  test_searcher.py test_searcher_acl_cursor.py
  test_searcher_pagination.py test_search_public_api.py
  test_search_schema.py
  test_serve_browse_route.py test_serve_search_route.py
  test_serve_changes_route.py test_serve_acl_routes.py
  test_api_client_ip.py test_api_auth_rate_limiter.py
  test_config.py
  conftest.py
CLAUDE.md                              # MODIFIED (PR #82): new
                                       # "Folder-filter planner choice
                                       # (#78, resolved)" paragraph
                                       # under the messages_recent_idx
                                       # section.
docs/handoffs/
  2026-05-22T0721-utc-folder-filter-pr-82.md   # this session's snapshot
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # prior (orphaned local file)
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                        # this file (post-session)
gui/                                   # unchanged
  src-tauri/
  src/
```

End of `folder-filter-coverage` session. PR #82 open against
`main` (`0d058ab`). Branch `test/78-folder-filter-coverage`
alive on local + remote until merge. Next: merge #82, then
either #79 (harness perf cleanup, fixes `_scan_actual_rows` and
`_pick_mid_cursor`) or measure the DISTINCT-induced Sort cost on
the broad folder-filter case to decide whether to file an
`EXISTS`-based browse SQL follow-up.
