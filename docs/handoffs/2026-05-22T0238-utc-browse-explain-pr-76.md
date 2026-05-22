# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-22 (post-session).** PR **#76**
> (`perf(browse): verify messages_recent_idx under ACL filter
> (closes #72)`) **open against `main`** on branch
> `perf/browse-explain-72` (head `e07d96b`). One commit,
> +972 / −1, three files added/modified. Full pytest suite
> **767 passed** (was 762 at session start; +5 plan-regression
> tests). mypy clean on touched files; 4 pre-existing `parser.py`
> errors carry forward unchanged. Awaiting review + merge.
>
> Prior session's PR **#74** (`refactor(search): expose Searcher
> pool metadata via accessor (#71)`) **merged** to `main` on
> 2026-05-22 as `77b264d`. No leftover work; branch
> `refactor/searcher-pool-metadata-accessor` already pruned.
>
> Issue **#75** filed during this session — separate mid-keyset
> perf bug surfaced during the #72 investigation
> (`OR COALESCE IS NULL` in the browse cursor predicate
> defeats the index range bound). Not in this PR's scope; see
> "What's next" below.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session closed the #72 carry-over from PR #70's review: verify
the planner picks `messages_recent_idx` for ACL-filtered browse
queries instead of falling to bitmap-scan + sort on multi-account
installs. The acceptance harness measured 200k-row synthetic
archives in three distribution shapes; **every probe uses option 1**
(index walk on `messages_recent_idx` + per-tuple `account_id`
filter). No covering index is needed. Filed #75 for the separate
mid-keyset perf issue surfaced during the investigation.

## What we shipped this session

### PR #76 — Verify `messages_recent_idx` under ACL filter (closes #72)

Branch: `perf/browse-explain-72` (head `e07d96b`).
Single commit. Test + harness only, no behaviour change.

#### Implementation

| SHA | What |
|---|---|
| `e07d96b` | `perf(browse)`: verify messages_recent_idx under ACL filter (closes #72). Adds `tests/acceptance/run_browse_explain.py` (synthetic 200k-row EXPLAIN harness with 4 ACL widths × 2 keyset positions × 3 distribution shapes). Adds `tests/test_api_browse_plan.py` (5 plan-regression tests pinning the index definition + eligibility). Updates `CLAUDE.md` with the planner-choice finding and a note about the separate mid-keyset issue (#75). |

#### Acceptance harness verdict

Across **3 distribution shapes** × **4 ACL widths** × **2 keyset positions**
on a synthetic 200,000-row archive:

```
probe                  plan family            filtered  exec ms
ACL=1 heavy | initial  index-walk (option 1)  7            0.09
ACL=1 heavy | mid      index-walk (option 1)  100014      28.28
ACL=half    | initial  index-walk (option 1)  3            0.06
ACL=all     | initial  index-walk (option 1)  0            0.06
```

Every probe → `Index Scan using messages_recent_idx`. No bitmap, no
full sort. **Option 2 (bitmap on `account_id`) never fires.**
Conclusion: no covering index is needed for the #72 dimension.

#### Plan-regression tests (5 new in `tests/test_api_browse_plan.py`)

1. `test_messages_recent_idx_definition_matches_design` — pins the
   indexdef text in `pg_indexes`. Each load-bearing token
   (COALESCE expression, `DESC NULLS LAST`, secondary `id DESC`)
   is asserted separately for clear failure messages.
2-4. `test_messages_recent_idx_is_eligible_for_list_messages_query`
   (ACL=1) / `_half_account_coverage` (ACL=half) /
   `_all_accounts` (ACL=all). Each drops every competing
   `messages` index — including `messages_pkey` via
   `DROP CONSTRAINT CASCADE` — inside a SAVEPOINT, then
   asserts `messages_recent_idx` serves the EXPLAIN plan with
   no full Sort node.
5. `test_plan_probe_savepoint_restores_dropped_indexes` —
   sanity-check that the SAVEPOINT rollback restores all 10
   `messages` indexes, so the other assertions remain
   meaningful and don't leak.

### Test deltas

```
backend pytest:    762 → 767  (+5 plan-regression tests)
mypy:              4 pre-existing parser.py errors (unchanged)
```

### Docs updates this session

- **CLAUDE.md** — added a paragraph in the "Schema essentials"
  section documenting the planner-choice finding for `messages_recent_idx`
  under the ACL filter (#72 resolved) and a follow-up paragraph
  on the mid-keyset OR-IS-NULL perf bug (#75 filed).
  Also added `run_browse_explain.py` to the `tests/acceptance/`
  file map.
- **README.md / ROADMAP.md** — unchanged (no user-facing change).

## What's next

### 1. Merge PR #76

Behaviour-preserving test + harness, ready for review. After merge:

```bash
git checkout main
git pull
git branch -d perf/browse-explain-72
# origin branch auto-deleted on merge; no `git push origin :branch` needed.
```

### 2. Pick the next piece

In order of recommendation:

- **#75** — Mid-keyset perf bug filed during this session. The
  cursor predicate's `OR COALESCE IS NULL` disjunction prevents
  Postgres from composing an index range bound, so deep keyset
  pages walk ~`total_rows / 2` tuples to find 51 matches.
  Acceptance: split the cursor into a dated-portion path
  (range-seekable) and a NULL-tail transition path; assert
  `Rows Removed by Filter` is bounded by `page_size`, not
  `~total_rows / 2`. Existing `test_api_browse_cursor.py`
  round-trip tests guard the semantics. The acceptance harness
  added in this PR is the natural before/after measurement
  vehicle.
- **PR-73 follow-up cleanup** — bundle the 5 minor polish
  items filed in the PR #73 handoff into one small PR: move
  inline `from ipaddress import IPv4Network` imports in
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
  initial load goes through `/v1/messages` (PR #70),
  `/v1/changes` is only the delta-fetch path. Worth resolving
  while the change is fresh.
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

1. **The acceptance harness is heavyweight by design.** Seeding
   200k rows takes ~4 seconds; a full run (8 probes × 200k rows)
   takes ~10s. It's intentionally a standalone script
   (`tests/acceptance/run_browse_explain.py`) rather than a
   pytest test — exactly like `run_recall_eval.py`, etc. Plan
   regression at unit-test scale is covered by
   `test_api_browse_plan.py` (which uses 150 rows and the
   SAVEPOINT-isolation technique).

2. **The unit tests assert *eligibility*, not *preference*.**
   At fixture scale Postgres legitimately prefers a PK-backward
   scan or a per-account index over `messages_recent_idx` — the
   tiny table fits in one page and the LIMIT short-circuit is
   irrelevant. Inflating fixtures 1000× to elicit the index walk
   would be misleading; the harness handles the preference
   question at production scale.

3. **`DROP CONSTRAINT messages_pkey CASCADE` is the only way to
   suppress the PK-backward scan.** Plain `DROP INDEX
   messages_pkey` is rejected by Postgres ("drop the constraint
   instead"). The CASCADE also removes every child FK
   referencing the PK — `failed_chunkings`, `message_chunks`,
   `message_labels`. The SAVEPOINT rollback restores all of
   them atomically. If a new child table is added with an FK to
   `messages.id`, the test will still work (the new FK is also
   restored on rollback) — but the rollback path becomes
   marginally more expensive. Add a comment to the new table's
   migration noting that it participates in this test if it
   becomes load-bearing for browse plans.

4. **The harness does not seed `message_chunks` /
   `attachment_chunks`.** Those tables don't participate in
   `list_messages` so the planner choice is unaffected. If a
   future query joins them into the browse path, extend the
   harness's seed step.

5. **Carried forward from prior sessions (still load-bearing):**
   - **PR #74** (`Searcher.get_pool_metadata` + `Searcher.config`)
     — merged this session start. The api/ layer (and any future
     MCP layer) must use the public accessors, not
     `searcher._cache` / `searcher._cfg`.
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

# If PR #76 is still open:
git checkout perf/browse-explain-72
gh pr view 76                              # check CI + review state

# After PR #76 is merged:
git checkout main
git pull
git branch -d perf/browse-explain-72

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                          # expect 767 passed
unset VIRTUAL_ENV && uv run mypy src/localmail     # 4 pre-existing parser.py errors

# Re-run the acceptance harness on demand:
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \
    --total-rows 200000 --accounts 5 --distribution skewed

# Pick next piece:
gh issue list --state open --limit 40
# Recommended: #75 (split keyset cursor: dated portion +
# NULL-tail transition; eliminate OR COALESCE IS NULL).
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
- **NEW from this session: `messages_recent_idx` planner choice
  (#72)** — the planner uses this index for ACL-filtered browse
  queries at production scale across all distribution shapes. No
  covering index is needed. The eligibility regression is pinned
  by `tests/test_api_browse_plan.py`; the operational preference
  is verified by `tests/acceptance/run_browse_explain.py`.
- **NEW from this session: mid-keyset perf bug (#75)** — the
  cursor predicate's `OR COALESCE IS NULL` defeats the index
  range bound. Deep pages filter ~`total_rows / 2` tuples. Do
  not paper over with stop-gap LIMIT widening; the fix is to
  split the cursor into dated-portion + NULL-tail transition.

## File map (as of branch HEAD `e07d96b`)

```
src/localmail/
  api/
    browse.py                          # unchanged; planner choice verified
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
tests/                                 # 767 passing
  acceptance/
    run_browse_explain.py              # NEW (PR #76): EXPLAIN harness
    run_recall_eval.py run_attachment_eval.py run_rrf_k_sweep.py
  test_api_browse_plan.py              # NEW (PR #76): 5 plan-regression tests
  test_searcher_pool_metadata.py       # PR #74 (merged this session)
  test_api_search_pagination.py        # PR #74 (merged this session)
  test_api_browse.py test_api_browse_cursor.py
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
  2026-05-22T0238-utc-browse-explain-pr-76.md   # this session's snapshot
NEXT_SESSION.md                       # this file (post-session)
gui/                                  # unchanged
  src-tauri/
  src/
```

End of `browse-explain-72` session. PR #76 open against `main`
(`e07d96b`). Branch `perf/browse-explain-72` alive on local +
remote until merge. Next: merge #76, then ship #75 (split keyset
cursor: dated portion + NULL-tail transition path; eliminate the
`OR COALESCE IS NULL` disjunction that defeats the index range
bound).
