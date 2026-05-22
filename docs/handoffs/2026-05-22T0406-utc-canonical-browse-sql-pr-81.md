# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-22 (post-session).** PR **#81**
> (`refactor(browse): canonical SQL emitter shared by
> tests/harness (closes #77)`) **open against `main`** on branch
> `chore/77-canonicalise-browse-sql` (head `6ec900d`). One
> commit, +182 / −105, five files. Full pytest suite **781
> passed** (no regressions vs prior 776; the +5 delta predates
> this session — likely uncounted from prior PRs, not caused by
> #77). mypy clean on touched files; 4 pre-existing `parser.py`
> errors carry forward unchanged. Awaiting review + merge.
>
> Prior session's PR **#80** (`perf(browse): row-comparison
> keyset + NULL-tail top-up (closes #75)`) **merged** to `main`
> on 2026-05-22 as `de06afe`. No leftover work; branch
> `perf/browse-cursor-split-75` already pruned locally.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session closed #77 — the SQL-drift risk between the inline test
SQL and the production emitter in `api/browse.py`. The browse path
now has a single canonical SQL emitter
(`BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql` + `build_where`)
that the unit-scale eligibility tests and the EXPLAIN harness import
directly. Refactor a SELECT shape or a WHERE-clause once → it lands
in every test and harness that depends on it.

## What we shipped this session

### PR #81 — Canonical browse SQL emitter (closes #77)

Branch: `chore/77-canonicalise-browse-sql` (head `6ec900d`).
Single commit. Pure refactor (no production behaviour change).

#### Implementation

| SHA | What |
|---|---|
| `6ec900d` | `refactor(browse)`: promote `_BROWSE_ROW_SQL` → `BROWSE_ROW_SQL_TEMPLATE`, `_build_where` → `build_where`, add `compose_browse_sql(folder_filter=…, where=…)`. `tests/test_api_browse_plan.py` + `tests/acceptance/run_browse_explain.py` drop inline SQL copies and compose via the production primitives. `pre75` harness variant keeps a local `_PRE75_BUGGY_WHERE` (deliberately divergent — reproduces the buggy planner choice) but reuses `BROWSE_ROW_SQL_TEMPLATE` for the SELECT shape so before/after stays apples-to-apples. CLAUDE.md gains a brief "canonical browse SQL emitter (#77)" note under the `messages_recent_idx` section. |

#### Acceptance — issue #77 criteria

| | criterion | satisfied by |
|---|---|---|
| ✅ | `list_messages`' SQL fragments live in named constants in `api/browse.py` | `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql` + `build_where` are module-level public symbols |
| ✅ | `test_api_browse_plan.py` and `run_browse_explain.py` import and compose those constants rather than re-typing them | both files import from `localmail.api.browse`; no inline `SELECT DISTINCT m.id …` strings remain |
| ✅ | a refactor of the production SQL automatically lands in every test that depends on the shape | drift is gone by construction — there is no inline copy to leave stale |

#### Tests

No new tests added — this is a refactor that preserves the contract.
The pre-existing 9 `build_where` tests from PR #80 still pin the
WHERE-clause shape; the 4 eligibility tests in
`test_api_browse_plan.py` now compose the production SQL directly
via the new public primitives.

### Test deltas

```
backend pytest:    776 → 781  (the +5 predates this session —
                                likely uncounted from prior PRs;
                                no regressions from #77)
mypy:              4 pre-existing parser.py errors (unchanged)
```

### Docs updates this session

- **CLAUDE.md** — added a "Canonical browse SQL emitter (#77)"
  paragraph under the `messages_recent_idx` section, documenting
  the three public primitives and the deliberate `pre75` exception.
- **README.md / ROADMAP.md** — unchanged (refactor is internal;
  no user-facing change). ROADMAP.md doesn't exist in this repo.

## What's next

### 1. Merge PR #81

Pure refactor; behaviour-preserving. After merge:

```bash
git checkout main
git pull
git branch -d chore/77-canonicalise-browse-sql
# origin branch auto-deleted on merge; no `git push origin :branch` needed.
```

### 2. Pick the next piece

In rough order of recommendation:

- **#78** — `browse-plan coverage: folder_ids JOIN branch not
  exercised by harness or unit tests`. Filed during the #72
  session. The harness seeds messages but no `message_labels`
  rows, so the planner's choice for the folder-filter branch is
  unmeasured. Now slightly easier with the canonical emitter
  from #77: add a `--folder-filter` mode to the harness +
  seed labels for one mailbox.
- **#79** — `run_browse_explain: _pick_mid_cursor scales linearly
  with --total-rows (OFFSET COUNT/2)`. The harness's OFFSET-based
  cursor picker becomes slow on huge archives. Swap for a
  percentile pick: `OFFSET (SELECT COUNT/2)` →
  `SELECT … OFFSET 0 LIMIT 1` after a `TABLESAMPLE` or a
  pre-computed median. Also fix the broken `_scan_actual_rows`
  while you're in the file (see open-decision #3 in this
  handoff — `actual rows=` substring never appears in modern
  EXPLAIN output).
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

1. **The `pre75` harness variant is deliberately divergent and
   only partially shares production primitives.** It composes
   `BROWSE_ROW_SQL_TEMPLATE` for the SELECT / FROM / ORDER BY
   shape (so the before/after comparison stays apples-to-apples
   if those ever change), but the WHERE clause is hard-coded as
   `_PRE75_BUGGY_WHERE` so the planner-choice delta is preserved.
   Don't "complete" the refactor by also composing the WHERE via
   `build_where` — there is no `build_where` mode that emits
   the buggy shape, and adding one would be a footgun in
   production.

2. **The new `compose_browse_sql(folder_filter=…, where=…)`
   helper is the one allowed way to do the `.format()` call on
   the template.** A caller that calls `BROWSE_ROW_SQL_TEMPLATE.format(...)`
   directly skips the `_FOLDER_JOIN_SQL` invariant and could
   mismatch the `{join}` placeholder. The harness `pre75` is the
   only place that calls `.format()` directly, and only because
   the WHERE clause is the divergent piece. Keep new consumers
   on `compose_browse_sql`.

3. **The harness's `_scan_actual_rows` is broken** (pre-existing,
   carried from prior sessions). It searches for `"actual rows="`
   but EXPLAIN ANALYZE emits `(actual time=X..Y rows=Z loops=W)`
   — no `actual rows=` substring. `actual_rows` is always 0 in
   the harness output. Not load-bearing for the #75 / #77
   verification (`rows_removed_by_filter` is the key metric and
   is parsed correctly). Worth fixing as part of #79 if someone
   touches the harness.

4. **Pyright (the VSCode language server) complains about
   `cur.execute(sql, ...)` where `sql` is a composed `str`.**
   This is psycopg's `LiteralString` typing — Pyright is stricter
   than mypy on this. mypy is the authoritative type check
   (per `pyproject.toml`) and is clean on `api/browse.py`. The
   IDE diagnostic is a non-blocker; if it becomes noisy, add a
   `# type: ignore[arg-type]` on the affected line or wrap the
   composed SQL in `psycopg.sql.SQL(...)`.

5. **Carried forward from prior sessions (still load-bearing):**
   - **PR #80** (`#75` — row-comparison keyset + NULL-tail top-up)
     — merged this session start. Mid-keyset browse pagination
     is range-bounded; do NOT rewrite the dated cursor predicate
     to the equivalent OR form (Postgres refuses to compose it
     as an Index Cond at production scale).
   - **PR #76** (`messages_recent_idx` planner choice verified)
     — ACL-filtered browse uses the index walk at production
     scale across all distribution shapes. Pinned by
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

# If PR #81 is still open:
git checkout chore/77-canonicalise-browse-sql
gh pr view 81                              # check CI + review state

# After PR #81 is merged:
git checkout main
git pull
git branch -d chore/77-canonicalise-browse-sql

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                          # expect 781 passed
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
# Recommended: #78 (folder_ids JOIN coverage — easier now with
# the canonical emitter from #77).
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
- **NEW from this session: canonical browse SQL emitter** (#77,
  PR #81) — `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql` +
  `build_where` in `src/localmail/api/browse.py` are the only
  authoritative SQL emitter for the browse path. The eligibility
  tests in `tests/test_api_browse_plan.py` and the EXPLAIN
  harness in `tests/acceptance/run_browse_explain.py` compose
  via these primitives. Do NOT re-introduce inline SQL copies
  in tests or harnesses; that's exactly the drift the refactor
  killed.
- **NEW from this session: `compose_browse_sql` is the allowed
  way to call `.format()` on the template** (#77, PR #81). Direct
  `BROWSE_ROW_SQL_TEMPLATE.format(...)` calls skip the
  `_FOLDER_JOIN_SQL` invariant. The harness `pre75` variant is
  the one allowed exception (deliberately divergent WHERE
  clause).

## File map (as of branch HEAD `6ec900d`)

```
src/localmail/
  api/
    browse.py                          # MODIFIED (PR #81):
                                       # BROWSE_ROW_SQL_TEMPLATE,
                                       # compose_browse_sql, build_where
                                       # all public.
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
tests/                                 # 781 passing
  acceptance/
    run_browse_explain.py              # MODIFIED (PR #81):
                                       # composes production
                                       # primitives; pre75 is the
                                       # one local divergent variant
    run_recall_eval.py run_attachment_eval.py run_rrf_k_sweep.py
  test_api_browse.py                   # MODIFIED (PR #81): import
                                       # build_where (no underscore)
  test_api_browse_plan.py              # MODIFIED (PR #81): drops
                                       # inline _LIST_MESSAGES_SQL /
                                       # _MID_KEYSET_SQL; composes
                                       # via production primitives
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
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # this session's snapshot
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                       # this file (post-session)
gui/                                  # unchanged
  src-tauri/
  src/
```

End of `canonical-browse-sql` session. PR #81 open against
`main` (`6ec900d`). Branch `chore/77-canonicalise-browse-sql`
alive on local + remote until merge. Next: merge #81, then
ship #78 (folder_ids JOIN coverage in the harness — easier
now with the canonical emitter from #77).
