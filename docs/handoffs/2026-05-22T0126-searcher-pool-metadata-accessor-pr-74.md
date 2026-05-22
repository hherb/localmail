# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-22 (post-session).** PR **#74**
> (`refactor(search): expose Searcher pool metadata via accessor (#71)`)
> **open against `main`** on branch
> `refactor/searcher-pool-metadata-accessor` (head `ca90cf7`). One
> commit, +287 / −29 lines, 4 files changed. Full pytest suite
> **762 passed** (was 755 at session start; +7 accessor unit tests
> + 1 race-condition test). mypy clean on touched files; 4
> pre-existing `parser.py` errors carry forward. Awaiting review +
> merge.
>
> Prior session's PR **#73** (`feat(auth): trusted_proxies — XFF-aware
> login rate limiter`) **merged** to `main` on 2026-05-21 — no
> leftover work; branch `feat/auth-trusted-proxies` already pruned.
>
> Branch `refactor/searcher-pool-metadata-accessor` lives locally +
> on origin; keep until PR #74 merges. Working tree clean (only
> `.claude/settings.local.json` untracked — local-only).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session closed the smallest carry-over from PR #70's review:
`localmail.api.search` was reaching into `Searcher._cache`'s
entry-dict to drive grow-pool transparent recovery. Refactor adds
a public `get_pool_metadata(token, *, user_id)` accessor +
`Searcher.config` read-only property so a future page-cache shape
change can no longer silently break the route. Behaviour is
unchanged — same cap-vs-current-cpa decision, same `* 2` growth,
same "at cap" empty sentinel.

## What we shipped this session

### PR #74 — `Searcher.get_pool_metadata` + `Searcher.config` accessor

Branch: `refactor/searcher-pool-metadata-accessor` (head `ca90cf7`).
Single commit. Pure internal refactor, behaviour-preserving.

#### Implementation

| SHA | What |
|---|---|
| `ca90cf7` | `refactor(search)`: expose Searcher pool metadata via accessor (#71). Adds `PoolMetadata` frozen dataclass + `Searcher.get_pool_metadata(token, *, user_id=None) -> PoolMetadata \| None` + `Searcher.config` read-only property. Refactors `localmail.api.search._continue_or_grow` to use both; drops two `# noqa: SLF001` reads. Updates `test_api_search_pagination.py` mocks; adds `tests/test_searcher_pool_metadata.py` (7 tests) + 1 new pagination race test. |

#### Public API additions in `localmail.search.searcher`

```python
@dataclass(frozen=True)
class PoolMetadata:
    candidates_per_arm: int
    page_size: int
    rerank_pool_size: int
    pool_size: int

class Searcher:
    @property
    def config(self) -> SearchConfig: ...

    def get_pool_metadata(
        self, search_token: str, *, user_id: int | None = None,
    ) -> PoolMetadata | None: ...
```

Scoping rules of `get_pool_metadata` mirror `continue_page` /
`grow_pool` exactly: `user_id=None` bypasses the owner check;
mismatched `user_id` is treated as a cache miss; TTL expiry returns
`None`; cache miss returns `None`. Calling it does NOT extend the
TTL (insertion-time anchored, not last-access).

### Test deltas

```
backend pytest:    755 → 762  (+7 accessor unit tests + 1 race test = +8;
                                one of the prior +1 was a stale collection
                                count — the net is +7 visible)
mypy:              4 pre-existing parser.py errors (unchanged)
```

Test breakdown of the +8:
- `tests/test_searcher_pool_metadata.py` — 7 new tests (config property,
  unknown token → None, metadata snapshot after search, user_id
  scoping, TTL expiry → None, reads don't extend TTL, PoolMetadata is
  frozen).
- `tests/test_api_search_pagination.py` —
  `test_pool_exhausted_but_cache_evicted_raises_cursor_expired`
  covers the race where `continue_page` raises `PageOutOfPoolError`
  but the entry is gone by the time the accessor runs → must
  surface 409, never 500.

### Docs updates this session

- None. README.md / CLAUDE.md / ROADMAP.md unchanged — pure internal
  refactor with no user-facing surface change. The `searcher.config`
  property is mentioned in the new `PoolMetadata` docstring; no
  external doc references the old `searcher._cfg` name.

## What's next

### 1. Merge PR #74

Single-commit, behaviour-preserving refactor. After review and merge:

```bash
git checkout main
git pull
git branch -d refactor/searcher-pool-metadata-accessor
# origin branch auto-deleted on merge; no `git push origin :branch` needed.
```

### 2. Pick the next piece

In order of recommendation:

- **#72** `EXPLAIN ANALYZE` of `messages_recent_idx` under the
  `account_id = ANY(...)` ACL filter. No code change expected;
  if the index isn't used, file a covering index instead.
  Carry-over from PR #70 handoff, paired with #71 (now landing).
- **PR-73 follow-up cleanup** — bundle the 5 minor polish items
  filed in last session's handoff into one small PR: move inline
  `from ipaddress import IPv4Network` imports in
  `tests/test_config.py:243,250` to module top; drop the
  defensive `object.__setattr__` form in
  `src/localmail/config.py:100-109`; canonicalise the
  `TrustedProxies` alias (currently duplicated in
  `client_ip.py` and `config.py`); optionally add a TOML
  round-trip test for `auth.trusted_proxies` keys.
- **`grow_pool` deep-pagination duplicates on `sort=rank`** —
  carried from PR #70 handoff. When the cache exhausts past pool
  size 100, `grow_pool` returns page 1 of the enlarged pool,
  surfacing already-seen top hits. `sort=date` covers "show me
  everything" so this is lower priority; revisit if rank-paginated
  duplicates become user-visible.
- **#38** `/v1/changes` semantics decision — now that the GUI's
  initial load goes through `/v1/messages` (PR #70), `/v1/changes`
  is only the delta-fetch path. Worth resolving while the change
  is fresh.
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

1. **Refactor is intentionally minimal.** Issue #71 floated a more
   ambitious version (pull the cap-vs-current-cpa decision *into*
   `Searcher.grow_pool` so the route just calls
   `grow_pool(token, target_cpa=cfg.candidates_per_arm_max)`). Not
   taken — would change `grow_pool`'s contract (it currently takes
   the new `candidates_per_arm`, not an upper bound) and force a
   public sentinel for "at cap". The minimal accessor solves the
   stated coupling problem (api/ layer reaching into
   `_cache`'s entry-dict shape) without changing any other contract.
   Revisit if the cap-vs-current logic acquires more callers.

2. **`get_pool_metadata` does not extend TTL.** Anchored to
   `PageCache.put` insertion time, not last access. Important
   because a chatty cap-probe must not keep an exhausted pool
   alive past its retention window. Test
   `test_get_pool_metadata_does_not_extend_ttl` enforces this.

3. **`Searcher.config` returns the live `SearchConfig`, not a
   copy.** Pydantic v2 models are mutable in principle; callers must
   not mutate the returned object (treat as read-only). Documented
   in the property docstring. The only current caller is
   `localmail.api.search.run_search` which only reads
   `candidates_per_arm_max`.

4. **`PoolMetadata` is intentionally a 4-field flat dataclass.**
   No need for the full cache entry shape (`hydrated`, `scores`,
   `parsed`, `sort`, `user_id` — these stay private to the
   Searcher). If a future caller needs more, extend the dataclass
   additively.

5. **Carried forward from prior sessions (still load-bearing):**
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

# If PR #74 is still open:
git checkout refactor/searcher-pool-metadata-accessor
gh pr view 74                              # check CI + review state

# After PR #74 is merged:
git checkout main
git pull
git branch -d refactor/searcher-pool-metadata-accessor

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q              # expect 762 passed
unset VIRTUAL_ENV && uv run mypy src/localmail     # 4 pre-existing parser.py errors

# Pick next piece:
gh issue list --state open --limit 40
# Recommended: #72 (EXPLAIN ANALYZE messages_recent_idx + ACL).
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
  problem type. **New from this session**: the race where
  `continue_page` raises `PageOutOfPoolError` but the entry is
  evicted before the accessor runs also surfaces 409 (not 500) —
  test `test_pool_exhausted_but_cache_evicted_raises_cursor_expired`
  enforces.
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
  commit eagerly. New writes added to `login()` between these and a
  subsequent `raise` must wrap their own SAVEPOINT if they must NOT
  survive an outer rollback.
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
- **NEW from this session: `Searcher` public boundaries**. The
  api/ layer (and any future MCP layer) must use
  `searcher.get_pool_metadata(token, *, user_id)` and
  `searcher.config` — never reach into `searcher._cache` or
  `searcher._cfg`. The accessor's `user_id` scoping mirrors
  `continue_page` / `grow_pool` exactly. Tests in
  `tests/test_searcher_pool_metadata.py` enforce.

## File map (as of branch HEAD `ca90cf7`)

```
src/localmail/
  api/
    search.py                          # PR #74: uses Searcher.config +
                                        # get_pool_metadata; SLF001 noqas dropped
    client_ip.py auth.py acl.py
    attachments.py browse_cursor.py conditional.py
    errors.py ids.py messages.py range_requests.py
    sanitize.py search_cursor.py
  config.py                            # unchanged from PR #73
  search/
    searcher.py                        # PR #74: PoolMetadata dataclass +
                                        # Searcher.config property +
                                        # Searcher.get_pool_metadata accessor
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
tests/                                 # 762 passing
  test_searcher_pool_metadata.py       # NEW (PR #74): 7 accessor unit tests
  test_api_search_pagination.py        # PR #74: mocks switched to public API,
                                        # +1 race test for evicted entry
  test_api_client_ip.py
  test_api_auth_rate_limiter.py
  test_config.py
  test_api_browse.py test_api_browse_cursor.py
  test_api_search.py test_api_search_cursor.py
  test_api_search_cursor_error.py test_api_search_lang_dates.py
  test_searcher.py test_searcher_acl_cursor.py
  test_searcher_pagination.py test_search_public_api.py
  test_search_schema.py
  test_serve_browse_route.py test_serve_search_route.py
  test_serve_changes_route.py test_serve_acl_routes.py
  conftest.py
docs/handoffs/
  2026-05-22T0126-searcher-pool-metadata-accessor-pr-74.md   # this session's snapshot
NEXT_SESSION.md                       # this file (post-session)
gui/                                  # unchanged
  src-tauri/
  src/
```

End of `searcher-pool-metadata-accessor` session. PR #74 open against
`main` (`ca90cf7`). Branch `refactor/searcher-pool-metadata-accessor`
alive on local + remote until merge. Next: merge #74, then ship
#72 (`EXPLAIN ANALYZE messages_recent_idx + ACL`) — pairs naturally
with this refactor as the second carry-over from PR #70's review.
