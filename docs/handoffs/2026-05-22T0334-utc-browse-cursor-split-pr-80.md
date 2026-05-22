# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-22 (post-session).** PR **#80**
> (`perf(browse): row-comparison keyset + NULL-tail top-up
> (closes #75)`) **open against `main`** on branch
> `perf/browse-cursor-split-75` (head `cc4b971`). One commit,
> +546 / −48, five files. Full pytest suite **776 passed** (was
> 767 at session start; +9 new tests). mypy clean on touched
> files; 4 pre-existing `parser.py` errors carry forward
> unchanged. Awaiting review + merge.
>
> Prior session's PR **#76** (`perf(browse): verify
> messages_recent_idx under ACL filter (closes #72)`) **merged**
> to `main` on 2026-05-22 as `cc9c87f`. No leftover work; branch
> `perf/browse-explain-72` already pruned locally.
>
> #75 is now closed by PR #80. The acceptance harness's
> `--predicate-form pre75` switch is available for ad-hoc
> before/after measurement.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session closed #75 — the mid-keyset browse pagination perf bug
filed during the prior #72 investigation. The cursor predicate now
uses SQL row comparison so Postgres composes it as a single
`Index Cond` on `messages_recent_idx` (range-bounded scan starting
AT the cursor). NULL-tail rows are reached via a separate top-up
query when the dated portion is exhausted.

## What we shipped this session

### PR #80 — Row-comparison keyset + NULL-tail top-up (closes #75)

Branch: `perf/browse-cursor-split-75` (head `cc4b971`).
Single commit. Behaviour-preserving (pagination semantics unchanged),
perf-critical.

#### Implementation

| SHA | What |
|---|---|
| `cc4b971` | `perf(browse)`: dated-cursor predicate now `ROW(COALESCE(internal_date, date_sent), m.id) < ROW(%s, %s)` (Postgres composes this as an Index Cond on `messages_recent_idx`). When the dated portion runs short of `limit + 1`, `list_messages` issues a NULL-tail top-up query so the user gets a full page. `_build_where` exposes four modes (initial / dated / null-tail-keyset / null-tail-topup) and is unit-tested per mode. Acceptance harness gains `--predicate-form {current,pre75}` for on-demand before/after measurement. |

#### Measurement (200,000-row, ACL=1 heavy, skewed, mid-keyset 51-row LIMIT)

| metric | pre-#75 | post-#75 | factor |
|---|---|---|---|
| `Rows Removed by Filter` | 100,014 | 13 | ≈7,700× |
| Execution time | 28.3ms | 0.072ms | ≈393× |
| `Buffers: shared hit` | ~500,027 | 424 | ≈1,180× |

The residual filter rows are bounded by the per-tuple ACL cost
(`page_size / acl_fraction`), not by table size. `ACL=1 light`
(3.75% of rows) shows 1,411 filtered — exactly what
`page_size / 0.0375 ≈ 1,333` predicts.

#### Why row comparison and not just removing `OR COALESCE IS NULL`?

The original hypothesis in the issue was to drop the trailing
`OR COALESCE IS NULL` disjunct. Necessary but not sufficient: the
remaining OR-form keyset (`expr < X OR (expr = X AND id < Y)`) still
degrades to a post-walk `Filter:` at production scale — Postgres
refuses to decompose a mixed-column OR into an index range bound
when an Index Scan alternative is on the table. Empirically the
OR-form at 50k rows shows the same ~25k `Rows Removed by Filter`
as the buggy three-disjunct form. The ROW form is what Postgres
actually optimizes — it gets a single `Index Cond:
ROW(COALESCE(...), id) < ROW($1, $2)` on the expression index.

#### Tests (9 new in this PR)

`tests/test_api_browse.py` (+8):

1. `test_dated_cursor_full_page_does_not_query_null_tail` —
   end-to-end: dated cursor with mixed dated+NULL data, full page
   from dated only, no NULL leakage into the dated predicate.
2. `test_dated_cursor_exhausted_tops_up_from_null_tail_in_one_page`
   — end-to-end: partial dated page + remaining slots from
   NULL-tail in one response.
3. `test_dated_cursor_at_boundary_returns_null_tail_only_page` —
   end-to-end: dated→NULL cursor flavour transition.
4. `test_build_where_initial_page_has_no_date_predicate` — pure
   function: cursor=None → WHERE is just the ACL filter.
5. `test_build_where_dated_cursor_uses_row_comparison_not_or_disjunction`
   — pure function: ROW form, no IS NULL, no OR.
6. `test_build_where_null_tail_cursor_uses_id_keyset` — pure
   function: NULL-tail keyset with `IS NULL AND id < %s`.
7. `test_build_where_null_tail_topup_has_no_id_predicate` — pure
   function: top-up mode with `IS NULL` and no id constraint.
8. `test_build_where_folder_clause_added_for_all_modes` — pure
   function: folder clause survives all four modes.

`tests/test_api_browse_plan.py` (+1):

9. `test_dated_cursor_predicate_composes_index_range_bound` —
   EXPLAIN-level: with competing indexes hidden via SAVEPOINT,
   the dated cursor predicate must produce an `Index Cond:` that
   references COALESCE; no `Filter:` line may contain COALESCE.
   This is the exact regression signature of #75.

### Test deltas

```
backend pytest:    767 → 776  (+9 tests)
mypy:              4 pre-existing parser.py errors (unchanged)
```

### Docs updates this session

- **CLAUDE.md** — moved the #75 paragraph from "follow-up" to
  "resolved". Documented why the OR form fails at production
  scale, why ROW comparison works, recorded the before/after
  measurement, and warned future contributors against rewriting
  back to OR. The `messages_recent_idx` planner-choice paragraph
  (post-#72) is unchanged.
- **README.md / ROADMAP.md** — unchanged (no user-facing change;
  perf is internal).

## What's next

### 1. Merge PR #80

Behaviour-preserving perf fix, ready for review. After merge:

```bash
git checkout main
git pull
git branch -d perf/browse-cursor-split-75
# origin branch auto-deleted on merge; no `git push origin :branch` needed.
```

### 2. Pick the next piece

In rough order of recommendation:

- **#77** — `test_api_browse_plan: inline SQL drift risk between
  tests/harness and api/browse.py`. Filed during the #72 session.
  This PR partly addresses it (both files now mirror the ROW
  form), but the drift risk remains structural. Fix: import
  `_build_where` from `api/browse.py` into the harness + plan
  tests so there is exactly one canonical SQL emitter. Small,
  contained.
- **#78** — `browse-plan coverage: folder_ids JOIN branch not
  exercised by harness or unit tests`. Also a #72-session
  follow-up. The harness seeds messages but no `message_labels`
  rows, so the planner's choice for the folder-filter branch is
  unmeasured. Add a `--folder-filter` mode to the harness +
  seed labels for one mailbox.
- **#79** — `run_browse_explain: _pick_mid_cursor scales linearly
  with --total-rows (OFFSET COUNT/2)`. The harness's OFFSET-based
  cursor picker becomes slow on huge archives. Swap for a
  percentile pick: `OFFSET (SELECT COUNT/2)` →
  `SELECT … OFFSET 0 LIMIT 1` after a `TABLESAMPLE` or a
  pre-computed median.
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

1. **Row comparison is the only form that works.** The OR-form
   keyset (which the issue suggested as the fix) does NOT actually
   compose as an Index Cond at production scale. Future
   contributors are warned in `CLAUDE.md`'s `messages_recent_idx`
   section; the pure-function test
   `test_build_where_dated_cursor_uses_row_comparison_not_or_disjunction`
   pins the SQL form. Do not rewrite to OR even though it's
   semantically equivalent.

2. **NULL-tail top-up is conditional, not unconditional.** Only
   runs when `cursor is not None AND cursor.ts is not None AND
   len(rows) < fetch_limit`. The initial page (cursor=None) does
   not run a top-up because the unrestricted query naturally
   returns NULL rows in the NULLS-LAST tail via LIMIT. Don't
   "simplify" the conditional into always-run; you'll double the
   query cost on the common case where dated has more than a
   page of matches.

3. **The harness's `_scan_actual_rows` is broken** (pre-existing).
   It searches for `"actual rows="` but EXPLAIN ANALYZE emits
   `(actual time=X..Y rows=Z loops=W)` — no `actual rows=`
   substring. `actual_rows` is always 0 in the harness output.
   Not load-bearing for the #75 verification (`rows_removed_by_filter`
   is the key metric and is parsed correctly). Worth fixing as part
   of #79 if someone touches the harness.

4. **`Index Cond:` line layout depends on Postgres version.** The
   plan-regression test in `test_api_browse_plan.py` asserts
   "Index Cond" string + "COALESCE" string presence anywhere on a
   matching line. If a future Postgres release changes the wording
   (e.g. "Index Quals:") the test fails loudly. That's the right
   failure mode — the assertion is a tripwire, not a tight
   constraint on phrasing.

5. **The `pre75` predicate-form in the harness is intentional
   technical debt.** It exists so the operator can reproduce the
   before/after measurement at any time. Don't delete it just
   because the bug is fixed; the proof-of-fix harness is the
   primary diagnostic for any future regression.

6. **Carried forward from prior sessions (still load-bearing):**
   - **PR #76** (`messages_recent_idx` planner choice verified)
     — merged this session start. ACL-filtered browse uses the
     index walk at production scale across all distribution
     shapes. The eligibility regression is pinned by
     `tests/test_api_browse_plan.py`.
   - **PR #74** (`Searcher.get_pool_metadata` + `Searcher.config`)
     — the api/ layer (and any future MCP layer) must use the
     public accessors, not `searcher._cache` / `searcher._cfg`.
   - `auth.trusted_proxies` (#73) — opt-in CIDR list governs
     both admission (is the immediate peer a trusted proxy?)
     and peeling (which XFF entries to skip). Empty default =
     unchanged behaviour. Do NOT also set
     `uvicorn --forwarded-allow-ips`.
   - Postgres-backed login rate limiter (#7, PR #69) — caps live
     in `LocalmailConfig.auth`; `_record_login_attempt` +
     `_maybe_sweep` commit eagerly.
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

# If PR #80 is still open:
git checkout perf/browse-cursor-split-75
gh pr view 80                              # check CI + review state

# After PR #80 is merged:
git checkout main
git pull
git branch -d perf/browse-cursor-split-75

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                          # expect 776 passed
unset VIRTUAL_ENV && uv run mypy src/localmail     # 4 pre-existing parser.py errors

# Re-run the acceptance harness on demand (current vs pre75):
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \
    --total-rows 200000 --accounts 5 --distribution skewed \
    --predicate-form current     # post-#75: filtered ~13 at mid-keyset

unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \
    --total-rows 200000 --accounts 5 --distribution skewed \
    --predicate-form pre75       # pre-#75: filtered ~100k at mid-keyset

# Pick next piece:
gh issue list --state open --limit 40
# Recommended: #77 (drift between harness/test SQL and api/browse.py).
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
- **NEW from this session: dated-cursor predicate MUST use ROW
  comparison (#75, PR #80)** — `ROW(COALESCE(internal_date,
  date_sent), m.id) < ROW(%s, %s)`. Do NOT rewrite to the
  equivalent OR form (`expr < X OR (expr = X AND id < Y)`) —
  Postgres refuses to decompose mixed-column ORs into an index
  range bound and the predicate degrades to a post-walk
  `Filter:` walking ~`total_rows / 2` tuples per page.
- **NEW from this session: NULL-tail top-up is conditional**
  (#75, PR #80) — only runs when `cursor is not None AND
  cursor.ts is not None AND len(rows) < fetch_limit`. Don't
  collapse the conditional; the initial page already naturally
  returns NULL rows via NULLS-LAST.

## File map (as of branch HEAD `cc4b971`)

```
src/localmail/
  api/
    browse.py                          # MODIFIED (PR #80): ROW-form
                                       # cursor predicate; NULL-tail
                                       # top-up; _build_where has 4 modes.
    search.py                          # unchanged from PR #74
    client_ip.py auth.py acl.py
    attachments.py browse_cursor.py conditional.py
    errors.py ids.py messages.py range_requests.py
    sanitize.py search_cursor.py
  config.py                            # unchanged from PR #73
  search/
    searcher.py                        # unchanged from PR #74
    arms.py chunking.py embed_worker.py embeddings.py
    extract_worker.py extractor.py lang_detect.py
    page_cache.py query.py reranker.py
  serve/                               # unchanged
    routes/
      auth.py messages.py search.py changes.py
      accounts.py attachments.py version.py
    app.py middleware.py
  cli.py daemon.py worker.py ...
migrations/                            # 0001 … 0019_api_login_attempts.sql
tests/                                 # 776 passing
  acceptance/
    run_browse_explain.py              # MODIFIED (PR #80):
                                       # --predicate-form {current,pre75}
    run_recall_eval.py run_attachment_eval.py run_rrf_k_sweep.py
  test_api_browse.py                   # MODIFIED (PR #80): +8 tests
  test_api_browse_plan.py              # MODIFIED (PR #80): +1 test
  test_api_browse_cursor.py            # unchanged from PR #70
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
docs/handoffs/
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md   # this session's snapshot
NEXT_SESSION.md                       # this file (post-session)
gui/                                  # unchanged
  src-tauri/
  src/
```

End of `browse-cursor-split-75` session. PR #80 open against
`main` (`cc4b971`). Branch `perf/browse-cursor-split-75` alive
on local + remote until merge. Next: merge #80, then ship #77
(canonicalise SQL emitter so the harness/plan tests/api drift
risk goes away).
