# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-23 (post-session).** PR **#86**
> (`perf(browse): swap folder-filter to EXISTS semi-join (closes
> #85)`) **open against `main`** on branch
> `perf/85-browse-distinct-vs-exists-benchmark` (head `39988b1`).
> One commit, +186 / −135, six files. Full pytest suite **802
> passed** (was 801; +3 new structural tests in test_api_browse.py,
> −2 retired folder_filter-kwarg tests). mypy clean on touched
> files; 4 pre-existing `parser.py` errors carry forward unchanged.
> Awaiting review + merge.
>
> Prior session's PR **#84** (`chore(config): canonicalise
> TrustedProxies + drop defensive setattr`) **merged** to `main`
> on 2026-05-23 as `a7643ee`. No leftover production work; branch
> `chore/pr73-followup-cleanup` pruned locally.
>
> Issue **#85** filed + closed by PR #86. Benchmark numbers
> recorded as a comment on the issue
> ([comment-4524648378](https://github.com/hherb/localmail/issues/85#issuecomment-4524648378)).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session picked up the carried-forward "DISTINCT → EXISTS browse
SQL benchmark" item from the PR #84 handoff
([docs/handoffs/2026-05-23T0308-utc-pr73-followup-pr-84.md](docs/handoffs/2026-05-23T0308-utc-pr73-followup-pr-84.md)).
Filed issue #85, added a `--folder-join-form {distinct,exists}` flag
to the acceptance harness, ran the before/after benchmark at 200k
rows × 5 accounts × skewed × broad folder (PG 18.1), measured
~45–50% buffer-hit reduction across every folder-filter probe,
switched production to EXISTS, retired the harness flag (production
composes via the primitives), and updated CLAUDE.md.

## What we shipped this session

### PR #86 — folder-filter EXISTS semi-join

Branch: `perf/85-browse-distinct-vs-exists-benchmark` (head `39988b1`).
Single commit. Production perf improvement, no wire/schema change.

#### Implementation

| SHA | What |
|---|---|
| `39988b1` | [src/localmail/api/browse.py](src/localmail/api/browse.py): `BROWSE_ROW_SQL_TEMPLATE` drops `SELECT DISTINCT` and the `{join}` slot; `compose_browse_sql` drops the `folder_filter` kwarg; `build_where(folder_ids=…)` emits `WHERE EXISTS (SELECT 1 FROM message_labels ml WHERE ml.message_id = m.id AND ml.mailbox_id = ANY(%s))` instead of the JOIN-side `ml.mailbox_id` predicate; `_FOLDER_JOIN_SQL` constant removed. [tests/test_api_browse.py](tests/test_api_browse.py): retired the two `compose_browse_sql_folder_filter_{true,false}` tests, added three new structural tests (`never_emits_message_labels_join`, `never_emits_select_distinct`, `build_where_emits_exists_subquery_for_folder_filter`). [tests/test_api_browse_plan.py](tests/test_api_browse_plan.py): updated docstrings + helper comments for the semi-join shape; kept the existing eligibility assertions. [tests/acceptance/run_browse_explain.py](tests/acceptance/run_browse_explain.py): updated call sites to `compose_browse_sql(where=…)`; removed `--folder-join-form` flag + `_VALID_FOLDER_JOIN_FORMS` + `_EXISTS_*` constants + `_exists_folder_filter_sql_and_params` (production primitives now emit this shape natively). [tests/test_browse_explain_harness.py](tests/test_browse_explain_harness.py): removed the 6 structural tests for the retired `_exists_folder_filter_sql_and_params` helper; pruned now-unused `pytest` and `datetime` imports. [CLAUDE.md](CLAUDE.md): rewrote the folder-filter notes to describe the EXISTS shape, the planner's selectivity-dependent inversion at narrow scales, and why the eligibility tests don't forbid Sort nodes. |

#### Benchmark — 200k rows × 5 accounts × skewed × broad folder (PG 18.1)

| probe | DISTINCT exec | EXISTS exec | DISTINCT buf hit | EXISTS buf hit | buf hit Δ |
|---|---:|---:|---:|---:|---:|
| folder=selective initial | 1.208 ms | 1.136 ms | 25,744 | 13,454 | **−48%** |
| folder=broad initial | 0.194 ms | 0.160 ms | 3,519 | 1,895 | **−46%** |
| folder=broad mid-keyset | 0.197 ms | 0.184 ms | 3,525 | 1,927 | **−45%** |
| folder=broad-across-accounts | 0.165 ms | 0.148 ms | 3,381 | 1,859 | **−45%** |

Structural plan diff: DISTINCT shape = `Limit → Unique → Incremental
Sort → Nested Loop` (3 nodes above the loop, sorting on all 9 projected
columns); EXISTS shape = `Limit → Nested Loop → Nested Loop Semi Join`
(zero ordering work above the semi-join). The semi-join also short-
circuits the labels lookup on first match per outer row (122 → 98
`message_labels_pkey` index searches for the broad-initial probe).
Full EXPLAIN diff + reproducibility command on
[#85 comment](https://github.com/hherb/localmail/issues/85#issuecomment-4524648378).

#### Acceptance — #85 issue body criteria

| # | criterion | status |
|---|---|---|
| 1 | `--folder-filter --folder-join-form exists` runs and emits same probe matrix as `distinct` | ✅ measured both; results recorded on #85 |
| 2 | side-by-side numbers from 200k × 5-account × skewed × broad folder recorded on #85 | ✅ |
| 3 | go/no-go decision documented on #85 | ✅ go |
| 4 | follow-up PR opened iff go | ✅ same PR (#86), bundled the production switch with the benchmark work since the change was small |

### Test deltas

```
backend pytest:    801 → 802  (+3 structural tests in tests/test_api_browse.py;
                               −2 retired folder_filter-kwarg tests;
                               +6 then −6 transient harness shape tests
                               for the retired _exists_folder_filter_*)
mypy:              4 pre-existing parser.py errors (unchanged);
                   clean on touched files
```

### Docs updates this session

- **README.md** — unchanged (no user-visible change; the SQL refactor
  is purely internal).
- **CLAUDE.md** — rewrote the "Canonical browse SQL emitter (#77)"
  and "Folder-filter planner choice (#78)" sections to describe the
  EXISTS shape, removed the "JOIN+DISTINCT Sort+Unique is inherent"
  note (no longer true), and added the new "at fixture scale the
  planner inverts the semi-join" caveat for the eligibility tests.
- **ROADMAP.md** — file does not exist in this repo; no update needed.

## What's next

### 1. Merge PR #86

Production perf improvement. After merge:

```bash
git checkout main
git pull
git branch -d perf/85-browse-distinct-vs-exists-benchmark
# origin branch auto-deleted on merge.
```

### 2. Pick the next piece

In rough order of recommendation:

- **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
  `main`. All in `gui/` (10 vite, 1 esbuild, 1 glib). The high-sev
  is vite dev-server (Arbitrary File Read via WebSocket), affects
  developers running `npm run dev` not end-users of the Tauri build.
  Worth a session against the `gui/` subproject — bump vite to the
  Dependabot-recommended version + verify the Tauri build still
  compiles. Blocked on no GUI CI (#18); manual build verification
  needed.
- **#38** `/v1/changes` semantics decision — now that the GUI's
  initial load goes through `/v1/messages` (PR #70), the cursor
  pagination is range-bounded (PR #80), and folder-filter is
  EXISTS-shaped (PR #86), `/v1/changes` is only the delta-fetch
  path. Worth resolving while the change is fresh.
- **`grow_pool` deep-pagination duplicates on `sort=rank`** —
  carried from PR #70 handoff. When the cache exhausts past pool
  size 100, `grow_pool` returns page 1 of the enlarged pool,
  surfacing already-seen top hits. `sort=date` covers "show me
  everything" so this is lower priority; revisit if rank-paginated
  duplicates become user-visible.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`).
- **#4 / #2 / #5** Search-perf follow-ups (model paths, CONCURRENT
  GIN build, batch INSERT for chunking).
- **#25** `websockets.legacy` DeprecationWarning — still blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on real ops data.

## Open decisions & risks

1. **DISTINCT-regression assertion at unit scale.** Considered
   tightening the folder-filter eligibility tests in
   `tests/test_api_browse_plan.py` to assert `not
   _has_full_sort_node(plan)` post-#85. Reverted because at fixture
   scale (50 rows/account × 3 accounts) the planner correctly
   INVERTS the semi-join — starts from `message_labels`, does an
   `Index Scan using messages_recent_idx on messages` keyed on `id =
   ml.message_id`, then Sorts to restore the ORDER BY. The Sort
   there is for *ordering*, not for *deduplication*. The
   DISTINCT-regression signature (`Unique` node + Sort over every
   projected column) only surfaces at scales where the date-ordered
   walk is preferred — which is what the acceptance harness covers,
   not the unit tests. If a future regression accidentally
   re-introduces DISTINCT, it will surface in the harness's buffer-
   hit numbers, not in the unit eligibility tests.

2. **Harness `--folder-join-form` flag retired.** The PR adds the
   flag, runs the benchmark, removes the flag. Alternative
   considered: keep it as a legacy comparison option (mirroring
   `--predicate-form pre75`). Rejected because (a) the DISTINCT
   shape is not a bug to demonstrate, just a slightly slower shape,
   and (b) two `--*-form` legacy flags would bloat the harness
   surface for a one-off benchmark. The methodology is preserved
   on #85; future regression-measurement would re-implement (or
   git-revert this PR on a benchmark branch).

3. **`compose_browse_sql` signature change.** Dropped the
   `folder_filter` kwarg entirely instead of leaving it as a
   deprecated no-op. Internal-only signature; the only callers
   were `_fetch_rows` (in the same file) and the
   tests/harnesses, all updated in this PR. If future external
   consumers materialised, the signature would need to be
   revisited — but `compose_browse_sql` is internal-ish (used by
   `tests/test_api_browse_plan.py` + `tests/acceptance/`), not a
   public API.

4. **Carried forward from prior sessions (still load-bearing):**
   - **PR #84** (`PR-73 follow-up`) — merged this session start.
     `TrustedProxies` canonical in `src/localmail/api/client_ip.py`;
     `AuthConfig.model_post_init` uses direct PrivateAttr assignment.
   - **PR #83** (`#79` — harness perf + parser fix) —
     `_mid_cursor_from_seed(cfg)` is pure (no `psycopg.Connection`);
     `_scan_actual_rows` parses PG≤17 / PG≥18 output formats.
     Versioned-pinned in `tests/test_browse_explain_harness.py`.
   - **PR #82** (`#78` — folder-filter plan coverage) —
     eligibility tests in `tests/test_api_browse_plan.py` cover the
     semi-join-shaped browse SQL across narrow / broad / multi
     folder filters. The harness verdict split (folderless vs
     folder-filter) is load-bearing: covering-index recommendation
     is folderless-only.
   - **PR #81** (`#77` — canonical browse SQL emitter) —
     `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql` +
     `build_where` in [src/localmail/api/browse.py](src/localmail/api/browse.py)
     are the only authoritative SQL emitter for the browse path.
     Tests + harness compose via the production primitives — do NOT
     re-introduce inline SQL.
   - **PR #80** (`#75` — row-comparison keyset + NULL-tail
     top-up) — mid-keyset browse pagination is range-bounded; do
     NOT rewrite the dated cursor predicate to the equivalent OR
     form.
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

# If PR #86 is still open:
git checkout perf/85-browse-distinct-vs-exists-benchmark
gh pr view 86                              # check CI + review state

# After PR #86 is merged:
git checkout main
git pull
git branch -d perf/85-browse-distinct-vs-exists-benchmark

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                         # expect 802 passed
unset VIRTUAL_ENV && uv run mypy src/localmail    # 4 pre-existing parser.py errors

# Verify the production SQL is EXISTS-shaped (#85):
grep -n 'SELECT DISTINCT\|JOIN message_labels' src/localmail/api/browse.py
# Expect: zero matches (only commentary in the docstring near line 85).
grep -n 'EXISTS (SELECT 1 FROM message_labels' src/localmail/api/browse.py
# Expect: one match in build_where.

# Re-run the benchmark (production EXISTS shape):
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \
  --total-rows 200000 --accounts 5 --distribution skewed \
  --folder-filter --json 2>/dev/null
# Expect: 4x index-walk (option 1) for folder-filter probes,
# buf_hit ~1,800-2,000 for the broad probes.

# Pick next piece:
gh issue list --state open --limit 40
# Top candidates: Dependabot triage (12 GUI vulns, 1 high),
# #38 /v1/changes semantics, GUI polish (#17/#18/#22/#24/#27/#28).
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
- **`TrustedProxies` alias is canonical in
  `src/localmail/api/client_ip.py`** (PR #84). `src/localmail/config.py`
  imports from there. Do NOT re-introduce a local alias definition
  in `config.py` — `grep -n 'TrustedProxies = tuple' src/localmail/`
  must return exactly one match.
- **`AuthConfig.model_post_init` uses direct PrivateAttr assignment**
  (PR #84). If `AuthConfig` is ever made `frozen=True`, the
  assignment will need to revert to
  `object.__setattr__(self, "_trusted_proxies_parsed", ...)`.
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
- **Canonical browse SQL emitter** (#77, simplified by #85) —
  `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql(where=…)` +
  `build_where` in [src/localmail/api/browse.py](src/localmail/api/browse.py)
  are the only authoritative SQL emitter for the browse path.
  The template has no `{join}` slot post-#85 — folder filtering
  lives in a `WHERE EXISTS` subquery inside `build_where`.
- **`compose_browse_sql(where=…)` is the allowed way to call
  `.format()` on the template** (#77, simplified by #85). Direct
  `BROWSE_ROW_SQL_TEMPLATE.format(...)` is now redundant (only one
  slot) but the helper is still the public path. The harness's
  `pre75` variant is the one allowed exception (deliberately
  divergent WHERE clause).
- **NEW from this session: folder-filter uses EXISTS semi-join**
  (#85, PR #86). `build_where(folder_ids=…)` emits `EXISTS (SELECT
  1 FROM message_labels …)` inside the WHERE clause. Do NOT
  re-introduce `SELECT DISTINCT` + `JOIN message_labels` — the
  benchmark on #85 showed ~50% more buffer hits per page on every
  folder-filter probe with that shape.
- **NEW from this session: at fixture scale (test_api_browse_plan
  eligibility tests) the planner inverts the semi-join** (#85, PR
  #86) — starts from `message_labels`, looks up matching messages
  by PK via `messages_recent_idx`, then Sorts to restore the ORDER
  BY. This is correct; it's why the eligibility tests do NOT
  assert `not _has_full_sort_node(plan)`. The DISTINCT-regression
  signature (`Unique` node + Sort over every projected column) only
  surfaces at scales the acceptance harness covers.
- **Folder-filter planner choice** (#78, PR #82) — at production
  scale, every folder-filter probe picks `Index Scan using
  messages_recent_idx` (selectivity-dependent at smaller scales).
  Folder-filter eligibility tests assert
  `Index Scan using messages_recent_idx on messages` is present
  and `Bitmap Heap Scan on messages` is absent.
- **Harness's mid-keyset cursor derives from SeedConfig** (#79,
  PR #83) — `_mid_cursor_from_seed(cfg)` is a pure helper
  returning `(_EPOCH_ANCHOR + days=date_span_days/2,
  total_rows // 2)`. No more OFFSET scan. `_build_probes` takes
  `SeedConfig` instead of `psycopg.Connection`.
- **`_scan_actual_rows` parses `actual time=... rows=N`** (#79,
  PR #83) — both PG ≤17 (`rows=N`) and PG ≥18 (`rows=N.NN`
  loop-averaged). The pre-#79 version searched for the literal
  `"actual rows="` substring which Postgres has never emitted.
  EXPLAIN output format is now versioned-pinned in
  `tests/test_browse_explain_harness.py` — future PG format
  changes will break these tests loudly.

## File map (as of branch HEAD `39988b1`)

```
src/localmail/
  api/browse.py                          # MODIFIED (PR #86):
                                          # - BROWSE_ROW_SQL_TEMPLATE: no
                                          #   SELECT DISTINCT, no {join} slot
                                          # - compose_browse_sql(*, where):
                                          #   dropped folder_filter kwarg
                                          # - build_where(folder_ids=…):
                                          #   emits WHERE EXISTS (...)
                                          #   instead of ml.mailbox_id =
                                          #   ANY(%s) JOIN predicate
                                          # - _FOLDER_JOIN_SQL constant removed
  config.py                              # unchanged (post-PR #84)
  api/client_ip.py                       # unchanged (canonical TrustedProxies)
  api/                                   # otherwise unchanged
  search/                                # unchanged
  serve/                                 # unchanged
  cli.py daemon.py worker.py ...         # unchanged
migrations/                              # 0001 … 0019_api_login_attempts.sql
tests/                                   # 802 passing
  test_api_browse.py                     # MODIFIED (PR #86):
                                          # - retired two old folder_filter-
                                          #   kwarg tests
                                          # - added three structural tests:
                                          #   never_emits_message_labels_join
                                          #   never_emits_select_distinct
                                          #   build_where_emits_exists_subquery
  test_api_browse_plan.py                # MODIFIED (PR #86):
                                          # - updated docstrings + helper
                                          #   comments for semi-join shape
                                          # - updated compose_browse_sql call
                                          #   sites to drop folder_filter kwarg
                                          # - eligibility assertions UNCHANGED
                                          #   (no Sort assertion added — see
                                          #   open decision #1)
  test_browse_explain_harness.py         # MODIFIED (PR #86):
                                          # - removed 6 tests for retired
                                          #   _exists_folder_filter_sql_*
                                          #   helper
                                          # - pruned now-unused pytest +
                                          #   datetime imports
  acceptance/
    run_browse_explain.py                # MODIFIED (PR #86):
                                          # - dropped --folder-join-form flag
                                          # - dropped _exists_folder_filter_*
                                          #   helper + _EXISTS_* constants
                                          # - updated compose_browse_sql call
                                          #   sites
                                          # - module docstring updated for the
                                          #   post-#85 EXISTS shape
    run_recall_eval.py
    run_attachment_eval.py
    run_rrf_k_sweep.py
  conftest.py
CLAUDE.md                                # MODIFIED (PR #86): folder-filter
                                          # notes rewritten for the EXISTS
                                          # shape + the fixture-scale
                                          # inversion caveat
docs/handoffs/
  2026-05-23T0755-utc-exists-semi-join-pr-86.md  # this session's snapshot
  2026-05-23T0308-utc-pr73-followup-pr-84.md     # prior
  2026-05-22T0942-utc-harness-cleanup-pr-83.md   # prior
  2026-05-22T0721-utc-folder-filter-pr-82.md     # prior
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # prior
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                          # this file (post-session)
gui/                                     # unchanged
  src-tauri/
  src/
```

End of `perf/85-browse-distinct-vs-exists-benchmark` session. PR #86
open against `main` (`39988b1`), closes #85. Branch alive on local
+ remote until merge. Next: merge #86, then either triage the 12
Dependabot vulnerabilities in `gui/` (1 high) or address #38
(`/v1/changes` semantics).
