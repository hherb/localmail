# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-23 (post-session).** PR **#84**
> (`chore(config): canonicalise TrustedProxies + drop defensive
> setattr (PR-73 follow-up)`) **open against `main`** on branch
> `chore/pr73-followup-cleanup` (head `c36734c`). One commit,
> +30 / −12, two files. Full pytest suite **801 passed** (was
> 800; +1 new `test_auth_trusted_proxies_toml_round_trip`).
> mypy clean on touched files; 4 pre-existing `parser.py`
> errors carry forward unchanged. Awaiting review + merge.
>
> Prior session's PR **#83** (`perf(harness): pure mid-cursor +
> parse PG18 actual rows (closes #79)`) **merged** to `main` on
> 2026-05-23 as `a0a0761`. No leftover production work; branch
> `perf/79-harness-cleanup` already pruned locally.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session bundled the four actionable polish items carried over
from the PR-73 trusted-proxies handoff
([docs/handoffs/2026-05-21T0553-trusted-proxies-pr-73.md](docs/handoffs/2026-05-21T0553-trusted-proxies-pr-73.md))
into a single small chore PR. The `TrustedProxies` type alias was
defined identically in both `config.py` and `api/client_ip.py`; the
duplicate-with-drift-risk is removed by canonicalising it in
`client_ip.py` (the type's natural home — a pure module with no
other localmail deps that defines the algorithm consuming the
type) and importing it in `config.py`. The defensive
`object.__setattr__` form in `AuthConfig.model_post_init` is
replaced with a direct PrivateAttr assignment that pydantic v2
supports natively. Two inline test imports moved to module top.
A new `test_auth_trusted_proxies_toml_round_trip` covers the
TOML → pydantic → parsed-network path that existing tests
exercised only via direct `AuthConfig(...)` construction.

## What we shipped this session

### PR #84 — PR-73 follow-up cleanup

Branch: `chore/pr73-followup-cleanup` (head `c36734c`).
Single commit. No production behaviour change.

#### Implementation

| SHA | What |
|---|---|
| `c36734c` | `src/localmail/config.py`: remove local `TrustedProxies = tuple[IPv4Network \| IPv6Network, ...]` (line 13) and the now-unused `IPv4Network` / `IPv6Network` imports; add `from localmail.api.client_ip import TrustedProxies` so `client_ip.py` is the single source of truth. `AuthConfig.model_post_init` now uses `self._trusted_proxies_parsed = tuple(...)` instead of `object.__setattr__(self, "_trusted_proxies_parsed", ...)` — pydantic v2 supports direct PrivateAttr assignment. `tests/test_config.py`: hoist `from ipaddress import IPv4Network` to module top, drop the two inline imports at lines 243 and 250. New `test_auth_trusted_proxies_toml_round_trip` writes a `[auth] trusted_proxies = [...]` TOML and asserts the parsed network tuple matches direct `AuthConfig(...)` construction. |

#### Acceptance — PR-73 handoff polish items

| # | item | source | status |
|---|---|---|---|
| 1 | `xff_last=<unparseable>` log field misleading | `client_ip.py:111-123` | **SKIPPED** — handoff explicitly tags this as "matches spec sample, nit only" |
| 2 | inline `IPv4Network` imports in test bodies | `tests/test_config.py:243,250` | ✅ hoisted to module top |
| 3 | `object.__setattr__` defensive PrivateAttr form | `config.py:100-109` | ✅ replaced with direct assignment |
| 4 | `TrustedProxies` alias duplicated | `config.py:13` ↔ `client_ip.py:19` | ✅ canonicalised in `client_ip.py` |
| 5 | no TOML round-trip test for trusted_proxies | (theoretical gap) | ✅ added `test_auth_trusted_proxies_toml_round_trip` |

#### Why `client_ip.py` is the canonical home for `TrustedProxies`

- `client_ip.py` is the pure module that defines the algorithm
  (`resolve_client_ip`) which *consumes* the type.
- `client_ip.py` has zero localmail-internal imports — it's the
  lowest module in the dependency graph that needs the alias.
- `config.py` importing from `localmail.api.client_ip` is the only
  config→api edge in the codebase. It's acceptable because
  `client_ip.py` is pure (zero localmail-internal imports) — config
  is the schema, the api/ layer is normally the consumer, but here
  the consumer happens to own the natural type definition because
  the type is fundamentally about IP networks. **Invariant**:
  `client_ip.py` must remain free of any `localmail.config`
  import — adding one would close a cycle through that line.
  Other api modules (`api/auth.py`, `api/search.py`) already
  import from `localmail.config` and that's fine; only
  `client_ip.py` is constrained.
- Future MCP / non-FastAPI consumers of `resolve_client_ip` get
  the alias automatically without going through `config.py`.

### Test deltas

```
backend pytest:    800 → 801  (+1 new test_auth_trusted_proxies_toml_round_trip
                               in tests/test_config.py)
mypy:              4 pre-existing parser.py errors (unchanged);
                   clean on touched files
```

### Docs updates this session

- **README.md** — unchanged (no user-visible change).
- **CLAUDE.md** — unchanged (no schema, API, or load-bearing
  convention change; the `auth.trusted_proxies` documentation
  in CLAUDE.md is already accurate post-#73).
- **ROADMAP.md** — file does not exist in this repo; no update
  needed.

## What's next

### 1. Merge PR #84

Polish/cleanup-only. After merge:

```bash
git checkout main
git pull
git branch -d chore/pr73-followup-cleanup
# origin branch auto-deleted on merge.
```

### 2. Pick the next piece

In rough order of recommendation:

- **Folder-filter `DISTINCT → EXISTS` benchmark (filed during #78
  work, not yet an issue)** — the current
  `SELECT DISTINCT m.id, ...` shape on the folder-filter SQL
  forces a post-join Sort+Unique pass over every projected
  column. The search arms in `src/localmail/search/arms.py` use
  the cleaner `EXISTS (SELECT 1 FROM message_labels …)`
  semi-join — no DISTINCT, no post-join sort. Worth measuring
  whether browsing should switch. Acceptance: benchmark showing
  how much the DISTINCT-induced Sort costs at 200k rows × broad
  folder, and whether `EXISTS` is materially faster. Open an
  issue if you take this on. The `tests/test_api_browse_plan.py`
  eligibility tests already document the DISTINCT Sort as
  expected (they assert the index walk is present but do not
  assert "no full sort") — they'd need their assertions
  loosened to allow either shape, then the harness
  `tests/acceptance/run_browse_explain.py` would need a new
  `--predicate-form exists` flavour for before/after comparison.
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

1. **`TrustedProxies` import direction.** This PR established
   `client_ip.py` as the canonical home and `config.py` imports
   from there. This means `config.py` now has one
   `from localmail.api.client_ip import …` line — the first
   `config → api/*` import in the codebase. Alternative (not
   chosen): keep canonical in `config.py`, have `client_ip.py`
   import from there. Rejected because `client_ip.py` is
   documented as "Transport-free — no FastAPI, no HTTP imports.
   Reusable from any future caller (MCP, etc.)" and adding a
   config dep there would erode that property. The chosen
   direction is a one-line config dep on a pure module, which
   is the smaller compromise. If future maintenance wants the
   opposite direction, swap the import — the tests don't care
   about the location.

2. **PrivateAttr direct-assignment vs `object.__setattr__`.**
   Pydantic v2 supports `self._private = value` natively. The
   prior `object.__setattr__` form was hedging against a
   hypothetical future `model_config = ConfigDict(frozen=True)`
   on `AuthConfig`. Removing the hedge is a deliberate
   simplification consistent with the rest of the codebase's
   pydantic models (none of which are frozen). If `AuthConfig`
   is ever frozen, `model_post_init` will need to be revisited.

3. **DISTINCT-induced Sort on folder-filter browse SQL** —
   carried from PR #82. Still not filed as an issue. The
   eligibility tests in `tests/test_api_browse_plan.py`
   document this as "expected" — they assert
   `Index Scan using messages_recent_idx on messages` is
   present and `Bitmap Heap Scan on messages` is absent, but
   DO NOT assert "no full sort" for the folder-filter case
   (the DISTINCT-induced sort is inherent to the JOIN+DISTINCT
   shape). Switching to `EXISTS` would remove the Sort but
   needs benchmarked-before-shipped.

4. **EXPLAIN output format is now versioned-pinned in tests.**
   `tests/test_browse_explain_harness.py` exercises both
   `(actual time=... rows=N loops=M)` (PG ≤17) and
   `(actual time=... rows=N.NN loops=M)` (PG ≥18) variants.
   If a future PG release changes the output format again,
   these tests break loudly — which is the desired signal,
   not a problem. `test_seed_config_defaults_match_module_constants`
   is the companion pin for the seed constants.

5. **`actual_rows` is informational but not load-bearing.** The
   harness verdict relies on `plan_family` and
   `rows_removed_by_filter` — both of which were correct even
   when `actual_rows` reported 0. So the #79 parser fix changes
   the *report* but not any verdict outcome. Don't rely on
   `actual_rows` for verdict logic going forward either — it's
   a sanity-check field.

6. **Test baseline drift (+1 vs prior session).** Accounted
   for: the +1 is `test_auth_trusted_proxies_toml_round_trip`
   in `tests/test_config.py`. (The +2 unexplained drift noted
   in the PR #82 handoff is still unaccounted-for but not
   regressed in this session — 786 → 800 → 801, where 800
   was correctly accounted for by PR #83's +14 harness tests.)

7. **Carried forward from prior sessions (still load-bearing):**
   - **PR #83** (`#79` — harness perf + parser fix) — merged
     this session start. `_mid_cursor_from_seed(cfg)` is pure
     (no `psycopg.Connection`); `_scan_actual_rows` parses
     PG≤17 / PG≥18 output formats. Versioned-pinned in
     `tests/test_browse_explain_harness.py`.
   - **PR #82** (`#78` — folder-filter plan coverage) —
     eligibility tests in `tests/test_api_browse_plan.py`
     cover the JOIN-shaped browse SQL across narrow / broad /
     multi folder filters. The harness verdict split
     (folderless vs folder-filter) is load-bearing: covering-
     index recommendation is folderless-only.
   - **PR #81** (`#77` — canonical browse SQL emitter) —
     `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql` +
     `build_where` in `src/localmail/api/browse.py` are the
     only authoritative SQL emitter for the browse path. Tests
     + harness compose via the production primitives — do NOT
     re-introduce inline SQL.
   - **PR #80** (`#75` — row-comparison keyset + NULL-tail
     top-up) — mid-keyset browse pagination is range-bounded;
     do NOT rewrite the dated cursor predicate to the
     equivalent OR form.
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

# If PR #84 is still open:
git checkout chore/pr73-followup-cleanup
gh pr view 84                              # check CI + review state

# After PR #84 is merged:
git checkout main
git pull
git branch -d chore/pr73-followup-cleanup

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                          # expect 801 passed
unset VIRTUAL_ENV && uv run mypy src/localmail     # 4 pre-existing parser.py errors

# Verify TrustedProxies is canonical in client_ip.py only:
grep -rn 'TrustedProxies = tuple' src/localmail/
# Expect: exactly one match in src/localmail/api/client_ip.py:19

# Pick next piece:
gh issue list --state open --limit 40
# Recommended: file an issue for the DISTINCT → EXISTS browse SQL
# benchmark on folder-filter (carried from PR #82), then either
# benchmark it or triage Dependabot vulnerabilities (12 open).
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
- **NEW from this session: `TrustedProxies` alias is canonical in
  `src/localmail/api/client_ip.py`** (PR #84). `src/localmail/config.py`
  imports from there. Do NOT re-introduce a local alias definition
  in `config.py` — `grep -n 'TrustedProxies = tuple' src/localmail/`
  must return exactly one match.
- **NEW from this session: `AuthConfig.model_post_init` uses direct
  PrivateAttr assignment** (PR #84). If `AuthConfig` is ever made
  `frozen=True`, the assignment will need to revert to
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

## File map (as of branch HEAD `c36734c`)

```
src/localmail/
  config.py                             # MODIFIED (PR #84):
                                        # - TrustedProxies alias removed
                                        #   (line 13); now imported from
                                        #   localmail.api.client_ip
                                        # - IPv4Network/IPv6Network imports
                                        #   from `ipaddress` dropped (only
                                        #   the alias used them)
                                        # - AuthConfig.model_post_init uses
                                        #   direct self._priv = ... assignment
                                        #   instead of object.__setattr__
  api/client_ip.py                      # unchanged: still the canonical
                                        # home for TrustedProxies (line 19)
  api/                                  # otherwise unchanged
  search/                               # unchanged
  serve/                                # unchanged
  cli.py daemon.py worker.py ...        # unchanged
migrations/                             # 0001 … 0019_api_login_attempts.sql
tests/                                  # 801 passing
  test_config.py                        # MODIFIED (PR #84):
                                        # - module-top `from ipaddress import
                                        #   IPv4Network` added
                                        # - two inline IPv4Network imports
                                        #   dropped from test bodies
                                        # - new test_auth_trusted_proxies_
                                        #   toml_round_trip
  acceptance/
    run_browse_explain.py
    run_recall_eval.py run_attachment_eval.py run_rrf_k_sweep.py
  test_browse_explain_harness.py
  test_api_browse_plan.py
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
  conftest.py
CLAUDE.md                               # unchanged
docs/handoffs/
  2026-05-23T0308-utc-pr73-followup-pr-84.md     # this session's snapshot
  2026-05-22T0942-utc-harness-cleanup-pr-83.md   # prior
  2026-05-22T0721-utc-folder-filter-pr-82.md     # prior
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # prior
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                         # this file (post-session)
gui/                                    # unchanged
  src-tauri/
  src/
```

End of `pr73-followup-cleanup` session. PR #84 open against `main`
(`c36734c`). Branch `chore/pr73-followup-cleanup` alive on local +
remote until merge. Next: merge #84, then either file the
DISTINCT → EXISTS browse SQL benchmark issue, triage Dependabot
vulnerabilities, or address #38 (`/v1/changes` semantics).
