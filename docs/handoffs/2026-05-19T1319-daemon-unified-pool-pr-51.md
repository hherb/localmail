# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-19 (end of session).** PR **#50** (body_lang
> partial index, closes #40) was merged at the start of this session as
> squash commit `3aa927a`. This session shipped **PR #51** (unified
> daemon connection pool, closes **#37** and **#9**) — branch
> `feat/daemon-unified-pool`, single commit `696d078`, **523 tests pass**
> (516 baseline + 7 new pool-sizing tests), mypy clean on touched files.
> PR #51 currently **OPEN** at session close.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `696d078` | `feat(daemon): unify connection-pool ceiling across all workers (closes #37, #9)` — single shared `Daemon.pool` for IDLE+poll+embed+extract; new `compute_daemon_pool_size()` helper; `DaemonConfig.pool_max_size` override knob; `run_extract_worker(*, pool, ...)` API. |

PR **#51** opened against `main` (status: **OPEN** at session close).

### Issues closed

- **#37** — `daemon: unify connection-pool ceiling across IDLE/poll +
  embed + extract workers`. The previous three independent sources
  (`self.pool` for IDLE/poll, a separate `_embed_pool`, and a raw
  `psycopg.connect()` per extract sweep) didn't share a budget; on hosts
  with many accounts the live-connection count could exceed Postgres
  `max_connections` without any single pool noticing. Now all four
  thread types borrow from `Daemon.pool` and the pool's `max_size` is
  derived from `compute_daemon_pool_size(n_accounts, run_embed,
  run_extract)` (or overridden via `DaemonConfig.pool_max_size`).
- **#9** — `serve: configurable ConnectionPool max_size + worker count`.
  `ServeConfig.pool_max_size` / `pool_min_size` were already in place
  on the HTTP server; this PR completes the picture by giving the
  daemon the same kind of operator-tuneable knob, so the two sides of
  the system match conceptually. (Note: `serve` `uvicorn_workers > 1`
  is still constrained by the in-memory login rate limiter — issue
  #7 — and was not touched here.)

### Concrete deliverables in PR #51

- [`src/localmail/db.py`](src/localmail/db.py) — adds `POOL_BASELINE_MIN`,
  `POOL_HEADROOM`, `_SLOTS_PER_ACCOUNT`, and the pure helper
  `compute_daemon_pool_size(n_accounts, run_embed, run_extract,
  baseline_min=..., headroom=...)`. No magic numbers.
- [`src/localmail/config.py`](src/localmail/config.py) — adds
  `DaemonConfig.pool_max_size: int | None = None` (auto-compute is
  opt-out, not opt-in).
- [`src/localmail/daemon.py`](src/localmail/daemon.py) — single shared
  pool; removed `self._embed_pool` and the inline `psycopg.connect`
  lambda for extract_worker. Startup INFO log records the chosen
  `max_size` + the inputs that produced it.
- [`src/localmail/search/extract_worker.py`](src/localmail/search/extract_worker.py)
  — `run_extract_worker(*, pool, cfg, stop_event)`. Uses
  `pool.connection()` per drain so the slot is released during the
  inter-sweep sleep (was previously holding a raw connection across
  the full poll interval). `_INITIAL_BACKOFF_S` / `_MAX_BACKOFF_S`
  named constants.
- [`tests/test_daemon_pool.py`](tests/test_daemon_pool.py) — 7 new
  tests: the pure formula (baseline floor, account scaling, embed-only
  vs extract-only symmetry), Daemon auto-compute, explicit
  `pool_max_size` override, single-pool invariant
  (`d._embed_pool is None`), and the `DaemonConfig` default.
- [`tests/test_extract_worker.py`](tests/test_extract_worker.py) — two
  loop tests updated to wrap a small `ConnectionPool(min=1, max=2)`
  instead of `conn_factory`.
- [`config.example.toml`](config.example.toml) — commented-out example
  of the new `[daemon] pool_max_size` knob.
- [`CLAUDE.md`](CLAUDE.md) — "Sync model" section now spells out the
  single-pool invariant and the auto-compute formula.

Test surface: 516 → 523 (+7 new pool-sizing tests). No baseline
mypy/test regressions introduced.

## What's next — concrete acceptance criteria

PR #51 needs to merge first. Once it does:

### 1. Merge PR #51 and clean up

```bash
gh pr view 51                       # confirm CI green
gh pr merge 51 --squash --delete-branch
git checkout main && git pull
```

### 2. Pick the next open issue

Top candidates (handoff-prioritised; #37 + #9 now closed):

- **#25** `websockets.legacy` deprecation. Likely a one-line `import`
  fix + a re-run of `test_e2e_serve.py` to confirm the warning is gone.
  Cheapest win on the list.
- **#5** Batch INSERT for chunking loop. Perf follow-up; useful once
  archives are large. Touches `embed_worker.py`.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on having real operational data showing which classes
  fire. Resist pre-emptive widening.
- **#33** Unify ID typing across endpoints (strings on the wire, ints
  in DB). Touches API routes; deserves its own focused PR.
- **#32** Attachment streaming — Range support, Content-Disposition,
  MIME hardening. Bigger scope.

### Other open issues (unchanged)

- **#38** `/v1/changes` semantics — initial backfill window decision.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`
  rendering).
- **#7** IP-based / global login rate limiter.
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.

## Open decisions & risks

1. **Pool formula is `2*N + workers + 2`, floored at 4.** The headroom
   of 2 covers ad-hoc CLI invocations (e.g. `localmail retry-failed`)
   running alongside the daemon. If real deployments see operators
   running parallel ad-hoc commands, bump `POOL_HEADROOM` or expose it
   via config. For now, `DaemonConfig.pool_max_size` provides the
   escape hatch.

2. **`run_extract_worker` API breaking change.** The keyword-only
   parameter changed from `conn_factory` to `pool`. The worker was
   only called from `daemon.py` and two tests, both of which were
   updated in this PR. There is no external consumer of this symbol
   to worry about.

3. **`Daemon._embed_pool` attribute removed.** The single-pool test
   in `tests/test_daemon_pool.py` uses `getattr(d, "_embed_pool",
   None) is None`, so removing the attribute keeps the test passing.
   Any external callers that introspected this would have to be
   updated, but it was always a private implementation detail.

4. **`rrf_k=60` is the centre of a flat plateau, not an empirically
   verified optimum** (#35 outcome). No measurable harm in changing it
   within [10, 180] on the current synthetic corpora. Sweep tool
   (`tests/acceptance/run_rrf_k_sweep.py`) retained for production
   tuning.

5. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
   `main` still need triage. Independent of this PR. Run
   `gh api repos/hherb/localmail/vulnerability-alerts` after merge to
   confirm the count hasn't drifted.

6. **`websockets.legacy` DeprecationWarning** (#25) still fires during
   `test_e2e_serve.py`. Pre-existing; tracked. Likely next session's
   first task.

7. **Pre-#41 single-operator upgrade.** Anyone running a pre-`4e2e2f1`
   build with a created API user will see empty `/v1/accounts` until
   they run `localmail grant-account USERNAME <each-account>`. README
   upgrade note is in place; flag loudly before any release.

8. **Transient allowlist intentionally narrow** (#47, still open). Only
   `TransientExtractorError`, `ConnectionError`, `TimeoutError`,
   `MemoryError`. Third-party classes (e.g.
   `requests.exceptions.ConnectionError`) are NOT builtins — tracked
   as #47. Defer until real-world observation data is available.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # feat/daemon-unified-pool if PR #51 not yet merged

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 523 passed

# Merge PR #51.
gh pr view 51                               # confirm CI green
gh pr merge 51 --squash --delete-branch
git checkout main && git pull

# Triage the next issue.
gh issue list --state open --limit 40
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  picks the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0017 are additive.** Re-running `init-db` on an
  older archive is safe (idempotent), but back up first if the archive
  is non-trivial.
- **`docling` is the Phase 2 extractor.** Install with
  `uv sync --extra extraction` if you need attachment-text extraction
  locally.
- **TLS for `localmail serve`** — `--bind 0.0.0.0` requires `--tls-cert`
  + `--tls-key`. `--no-tls` is only honoured on `127.0.0.1`. Use
  `localmail rotate-tls` to generate a self-signed cert.
- **First-time `body_lang` install** — run `localmail lang-backfill`
  (or `embed-backfill`, which drains both queues) once after upgrade so
  `lang:` queries return rows.
- **ACL upgrade**: post-0016, new API users have **no grants**. Run
  `localmail grant-account USERNAME ACCOUNT_NAME` once per pair.
- **nh3 style-attribute whitespace**: nh3 emits compact
  `color:red` (no space after `:`); never assert exact whitespace in
  sanitiser tests — assert that the property and value both survived.
- **nh3 `attribute_filter` ordering**: the callback runs *after* the
  tag/attribute allowlist check but *before* the URL-scheme check.
  Schemes that aren't in `url_schemes` are stripped before the filter
  sees them, so anything the filter must reach must be in
  `_ALLOWED_URL_SCHEMES`.
- **extract_worker transient classification**: an exception is
  transient iff `isinstance(e, TransientExtractorError)` OR an instance
  of `(ConnectionError, TimeoutError, MemoryError)` appears anywhere in
  its `__cause__` / `__context__` chain. Third-party HTTP/IO classes
  (e.g. `requests.exceptions.ConnectionError`) are NOT in the set —
  extractors must raise `TransientExtractorError` explicitly to opt
  them in. Add to `_TRANSIENT_EXC_TYPES` only after seeing the class in
  real operations (avoid pre-emptive widening).
- **`<a href>` deny schemes** (#48): `_HREF_DENY_SCHEMES = ("cid:",
  "data:")`. Both schemes must remain in `_ALLOWED_URL_SCHEMES` so they
  reach the `attribute_filter` for img/src handling (cid → attachment
  URL rewrite; data:image/... validated by `_DATA_IMAGE_RE`). Any new
  URL scheme added to `_ALLOWED_URL_SCHEMES` that a browser would
  dereference on `<a href>` MUST be considered for parallel addition
  to `_HREF_DENY_SCHEMES`. The deny prefix match strips leading C0
  controls + ASCII whitespace first (mirrors WHATWG URL parser; see
  `_LEADING_URL_TRIM_RE`).
- **RRF fusion is robust but un-tuned against production data** (#35
  outcome): synthetic corpora are insensitive to `rrf_k` because one
  arm dominates rank-1. Don't conclude that the constant doesn't
  matter — only that the synthetic harness can't measure it. Use
  `tests/acceptance/run_rrf_k_sweep.py --corpus {multilingual,
  attachment}` to re-measure against any new corpus before tuning.
- **`body_lang` worker index** (#40): the lang-detect claim query
  needs `messages_body_lang_pending_idx` to avoid seq-scan as archives
  grow. The schema test in `tests/test_search_schema.py` enforces both
  the index's existence and its planner eligibility — if the worker
  query shape ever drifts (new predicate columns, different ORDER BY),
  update both the migration's WHERE clause and the EXPLAIN test
  together.
- **Daemon pool sizing** (#37, this PR): all daemon threads share
  `Daemon.pool`. Its `max_size` is auto-computed via
  `db.compute_daemon_pool_size(...)` unless `DaemonConfig.pool_max_size`
  is set. The chosen value is logged at startup. When adding a NEW
  long-running worker thread to the daemon, bump the formula (slot
  per worker) — not just the headroom — so the contract stays exact.

## File map (post-#51, on branch `feat/daemon-unified-pool`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py attachments.py auth.py errors.py messages.py
    sanitize.py
    search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/                          # auth, accounts, messages, attachments,
                                     # changes, search, version (all ACL-aware)
  daemon.py                          # single shared self.pool for ALL workers
  db.py                              # compute_daemon_pool_size() + constants
  search/
    arms.py chunking.py embed_worker.py embeddings.py
    extractor.py                     # TransientExtractorError (#36)
    extract_worker.py                # now takes pool=, not conn_factory= (#37)
    lang_detect.py page_cache.py query.py reranker.py searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0017_messages_body_lang_pending_index.sql
tests/
  acceptance/
    run_recall_eval.py               # Phase 1 multilingual gate
    run_attachment_eval.py           # Phase 2 attachment gate
    run_rrf_k_sweep.py               # #35 sweep harness
  test_daemon_pool.py                # #37 pool-sizing contract (new)
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of daemon-unified-pool session. Single commit `696d078` on
`feat/daemon-unified-pool`; PR #51 open. Merge it, then pick #25
(websockets deprecation — likely a one-liner), or move on to #5 /
#33 / #32 / #47.
