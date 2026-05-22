# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-22 (post-session).** PR **#83**
> (`perf(harness): pure mid-cursor + parse PG18 actual rows (closes #79)`)
> **open against `main`** on branch `perf/79-harness-cleanup`
> (head `c651d1b`). One commit, +243 / −29, two files. Full
> pytest suite **800 passed** (was 786; +14 new tests in
> `test_browse_explain_harness.py`). mypy clean on touched files;
> 4 pre-existing `parser.py` errors carry forward unchanged.
> Awaiting review + merge.
>
> Prior session's PR **#82** (`test(browse): folder-filter plan
> coverage (closes #78)`) **merged** to `main` on 2026-05-22 as
> `a806e9b`. No leftover production work; branch
> `test/78-folder-filter-coverage` already pruned locally.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session closed #79 — the accepted browse-plan harness had two
latent defects that compounded at scale. (1) `_pick_mid_cursor`
executed `OFFSET (SELECT COUNT(*)/2 FROM messages) LIMIT 1` to mint
the 50th-percentile keyset cursor — fine at 100k rows, expensive
on 5M+. (2) `_scan_actual_rows` searched EXPLAIN output for the
literal substring `"actual rows="` — which Postgres has never
emitted — so every harness probe reported `actual_rows=0`. Both
are now fixed and pinned by 14 new unit tests.

## What we shipped this session

### PR #83 — Harness perf + parser fix (closes #79)

Branch: `perf/79-harness-cleanup` (head `c651d1b`).
Single commit. No production code change — test harness only.

#### Implementation

| SHA | What |
|---|---|
| `c651d1b` | Replace `_pick_mid_cursor(conn)` with `_mid_cursor_from_seed(cfg: SeedConfig)` → `(_EPOCH_ANCHOR + timedelta(days=cfg.date_span_days / 2), cfg.total_rows // 2)`. Pure function, no SQL. `_build_probes` now takes `SeedConfig` instead of `psycopg.Connection`; `main()` threads it through. Fix `_scan_actual_rows` to anchor on `actual time=` and parse the `rows=` field after it — handles both PG ≤17 (`rows=N`) and PG ≥18 (`rows=N.NN` loop-averaged) emissions, and sidesteps the planner-estimate `rows=N` in the earlier `cost=…` group. 14 new tests in `tests/test_browse_explain_harness.py` pin both helpers. |

#### Acceptance — issue #79 criteria

| | criterion | satisfied by |
|---|---|---|
| ✅ | `_pick_mid_cursor` no longer reads from `messages` | replaced by `_mid_cursor_from_seed(cfg)` — pure, no `psycopg.Connection` parameter |
| ✅ | The mid-keyset probe still lands at ~50% of the seeded date range | unit test `test_mid_cursor_uses_anchor_plus_half_span`; operational harness run at 100k rows shows mid-keyset probes still pick `Index Scan using messages_recent_idx` with same buffer-hit profile as pre-fix |
| ✅ | Harness wall-clock unchanged at 100k rows; faster at 5M+ | the new derivation is O(1) arithmetic; at 100k rows the savings are sub-millisecond noise, but the OFFSET scan is gone entirely so larger seeds skip the `SELECT COUNT(*)` + offset-scan steps |

#### Operational verdict (production scale, 100k rows × 5 accounts × skewed × `--folder-filter`)

```
ACL=1 heavy | initial               index-walk (option 1)   actual_rows=51  0.095ms
ACL=1 heavy | mid                   index-walk (option 1)   actual_rows=51  0.084ms
ACL=1 light | initial               index-walk (option 1)   actual_rows=51  0.659ms
ACL=1 light | mid                   index-walk (option 1)   actual_rows=51  0.472ms
ACL=half | initial                  index-walk (option 1)   actual_rows=51  0.049ms
ACL=half | mid                      index-walk (option 1)   actual_rows=51  0.051ms
ACL=all | initial                   index-walk (option 1)   actual_rows=51  0.048ms
ACL=all | mid                       index-walk (option 1)   actual_rows=51  0.049ms
ACL=1 heavy | initial | sel=5%      index-walk (option 1)   actual_rows=51  1.029ms
ACL=1 heavy | initial | broad=50%   index-walk (option 1)   actual_rows=51  0.163ms
ACL=1 heavy | mid | broad=50%       index-walk (option 1)   actual_rows=51  0.162ms
ACL=all | initial | broad-acrossall index-walk (option 1)   actual_rows=51  0.159ms
```

`actual_rows=51` on every probe is the visible signal that the parser
fix worked — before #79, this column was always 0.

Smaller-scale runs (5k rows × 3 accounts × balanced) still surface
option 2 on narrow folders — the PR #82 expectation is preserved.

### Test deltas

```
backend pytest:    786 → 800  (+14 new harness-helper unit tests in
                               tests/test_browse_explain_harness.py)
mypy:              4 pre-existing parser.py errors (unchanged);
                   clean on touched files
```

### Docs updates this session

- **README.md** — unchanged (no user-visible change; the README only
  references the search-related harnesses).
- **CLAUDE.md** — unchanged (the prior session's "Folder-filter planner
  choice (#78, resolved)" paragraph still flagged the harness's
  `_scan_actual_rows` as broken; that flag could be removed but it's
  carried by NEXT_SESSION.md, not CLAUDE.md).
- **ROADMAP.md** — file does not exist in this repo; no update needed.

## What's next

### 1. Merge PR #83

Test/docs-only. After merge:

```bash
git checkout main
git pull
git branch -d perf/79-harness-cleanup
# origin branch auto-deleted on merge.
```

### 2. Pick the next piece

In rough order of recommendation:

- **Folder-filter `DISTINCT → EXISTS` benchmark (filed during #78 work,
  not yet an issue)** — the current `SELECT DISTINCT m.id, ...` shape on
  the folder-filter SQL forces a post-join Sort+Unique pass over every
  projected column. The search arms in `src/localmail/search/arms.py`
  use the cleaner `EXISTS (SELECT 1 FROM message_labels …)` semi-join —
  no DISTINCT, no post-join sort. Worth measuring whether browsing
  should switch. Acceptance: benchmark showing how much the
  DISTINCT-induced Sort costs at 200k rows × broad folder, and whether
  `EXISTS` is materially faster. Open an issue if you take this on.
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

1. **DISTINCT-induced Sort on folder-filter browse SQL** —
   carried from PR #82. Not filed as an issue yet. The
   eligibility tests in `tests/test_api_browse_plan.py`
   document this as "expected" — they assert
   `Index Scan using messages_recent_idx on messages` is
   present and `Bitmap Heap Scan on messages` is absent, but
   DO NOT assert "no full sort" for the folder-filter case
   (the DISTINCT-induced sort is inherent to the JOIN+DISTINCT
   shape). Switching to `EXISTS` would remove the Sort but
   needs benchmarked-before-shipped.

2. **EXPLAIN output format is now versioned-pinned in tests.** The
   new tests in `tests/test_browse_explain_harness.py` exercise
   both `(actual time=... rows=N loops=M)` (PG ≤17) and
   `(actual time=... rows=N.NN loops=M)` (PG ≥18) variants. If a
   future PG release changes the output format again, these tests
   break loudly — which is the desired signal, not a problem.
   `test_seed_config_defaults_match_module_constants` is the
   companion pin for the seed constants.

3. **`actual_rows` is now informational but not load-bearing.** The
   harness verdict relies on `plan_family` and
   `rows_removed_by_filter` — both of which were correct even when
   `actual_rows` reported 0. So the parser fix changes the *report*
   but not any verdict outcome. Don't rely on `actual_rows` for
   verdict logic going forward either — it's a sanity-check field.

4. **Test baseline drift (+14 vs prior session).** All accounted
   for: the +14 are the new tests in
   `tests/test_browse_explain_harness.py`. (The +2 unexplained
   drift noted in the PR #82 handoff is still unaccounted-for but
   not regressed in this session.)

5. **Carried forward from prior sessions (still load-bearing):**
   - **PR #82** (`#78` — folder-filter plan coverage) — merged
     this session start. Eligibility tests in
     `tests/test_api_browse_plan.py` cover the JOIN-shaped browse
     SQL across narrow / broad / multi folder filters. The
     harness verdict split (folderless vs folder-filter) is
     load-bearing: covering-index recommendation is folderless-only.
   - **PR #81** (`#77` — canonical browse SQL emitter) —
     `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql` +
     `build_where` in `src/localmail/api/browse.py` are the only
     authoritative SQL emitter for the browse path. Tests +
     harness compose via the production primitives — do NOT
     re-introduce inline SQL.
   - **PR #80** (`#75` — row-comparison keyset + NULL-tail top-up)
     — mid-keyset browse pagination is range-bounded; do NOT
     rewrite the dated cursor predicate to the equivalent OR form.
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

# If PR #83 is still open:
git checkout perf/79-harness-cleanup
gh pr view 83                              # check CI + review state

# After PR #83 is merged:
git checkout main
git pull
git branch -d perf/79-harness-cleanup

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                          # expect 800 passed
unset VIRTUAL_ENV && uv run mypy src/localmail     # 4 pre-existing parser.py errors

# Re-run the acceptance harness; actual_rows should now be non-zero:
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \
    --total-rows 100000 --accounts 5 --distribution skewed --folder-filter --json \
  | python3 -c "import json,sys; d=json.loads(sys.stdin.read().split('{',1)[1].rsplit('}',1)[0].join(['{','}'])); print('\n'.join(f\"  {p['name']:55s} family={p['plan_family']} actual_rows={p['actual_rows']}\" for p in d['probes']))"
# Expect: every probe → actual_rows=51, family=index-walk (option 1)

# Or at small scale to see option 2 fire on narrow selectivity (correct):
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \
    --total-rows 5000 --accounts 3 --distribution balanced --folder-filter
# Expect: VERDICT (folder-filter) reports mixed option 1 / option 2;
#         VERDICT (folderless) reports option 1 only.

# Pick next piece:
gh issue list --state open --limit 40
# Recommended: file an issue for the DISTINCT → EXISTS browse SQL
# benchmark on folder-filter (carried from PR #82), then either
# benchmark it or move to PR-73 cleanup / Dependabot triage.
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
- **Folder-filter planner choice** (#78, PR #82) — at production
  scale, every folder-filter probe picks `Index Scan using
  messages_recent_idx` (selectivity-dependent at smaller scales).
  Folder-filter eligibility tests assert
  `Index Scan using messages_recent_idx on messages` is present
  and `Bitmap Heap Scan on messages` is absent, but DO NOT assert
  "no full sort" — the DISTINCT-induced sort is inherent.
- **NEW from this session: harness's mid-keyset cursor derives from
  SeedConfig** (#79, PR #83) — `_mid_cursor_from_seed(cfg)` is a
  pure helper returning `(_EPOCH_ANCHOR + days=date_span_days/2,
  total_rows // 2)`. No more OFFSET scan. `_build_probes` takes
  `SeedConfig` instead of `psycopg.Connection`.
- **NEW from this session: `_scan_actual_rows` parses
  `actual time=... rows=N`** (#79, PR #83) — both PG ≤17 (`rows=N`)
  and PG ≥18 (`rows=N.NN` loop-averaged). The pre-#79 version
  searched for the literal `"actual rows="` substring which
  Postgres has never emitted. EXPLAIN output format is now
  versioned-pinned in `tests/test_browse_explain_harness.py` —
  future PG format changes will break these tests loudly.

## File map (as of branch HEAD `c651d1b`)

```
src/localmail/                          # unchanged this session
  api/                                  # unchanged
  search/                               # unchanged
  serve/                                # unchanged
  cli.py daemon.py worker.py ...        # unchanged
migrations/                             # 0001 … 0019_api_login_attempts.sql
tests/                                  # 800 passing
  acceptance/
    run_browse_explain.py               # MODIFIED (PR #83):
                                        # _pick_mid_cursor REMOVED,
                                        # replaced by pure
                                        # _mid_cursor_from_seed(cfg);
                                        # _scan_actual_rows now parses
                                        # PG≤17 / PG≥18 output formats;
                                        # _build_probes signature changed
                                        # to take SeedConfig (no Conn).
    run_recall_eval.py run_attachment_eval.py run_rrf_k_sweep.py
  test_browse_explain_harness.py        # NEW (PR #83): 14 unit tests
                                        # pinning _mid_cursor_from_seed
                                        # and _scan_actual_rows behavior
                                        # against representative
                                        # SeedConfigs and EXPLAIN output
                                        # samples.
  test_api_browse_plan.py               # unchanged from PR #82
  test_api_browse.py test_api_browse_cursor.py
  test_searcher_pool_metadata.py
  test_api_search_pagination.py
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
CLAUDE.md                               # unchanged
docs/handoffs/
  2026-05-22T0942-utc-harness-cleanup-pr-83.md   # this session's snapshot
  2026-05-22T0721-utc-folder-filter-pr-82.md     # prior
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # prior
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                         # this file (post-session)
gui/                                    # unchanged
  src-tauri/
  src/
```

End of `harness-cleanup` session. PR #83 open against `main`
(`c651d1b`). Branch `perf/79-harness-cleanup` alive on local +
remote until merge. Next: merge #83, then either file the
DISTINCT → EXISTS browse SQL benchmark issue, do the PR-73
follow-up cleanup, or triage Dependabot.
