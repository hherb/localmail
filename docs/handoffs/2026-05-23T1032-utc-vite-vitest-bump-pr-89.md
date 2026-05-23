# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-23T1032 UTC (post-session).** PR **#89**
> (`chore(deps): bump vite to ^6.4.2 + vitest to ^3.2.4 (clear 11
> Dependabot npm alerts)`) **open against `main`** on branch
> `chore/bump-vite-vitest-dependabot` (head `3f673bb`). Single
> commit, +335 / −1217 across two files (gui/package.json +
> gui/package-lock.json). `npm audit` reports **0
> vulnerabilities** post-bump; `npm test` 271/271 pass;
> `npm run build` clean (vite v6.4.2, 793ms); `npm run check`
> (svelte-check) 0 errors / 0 warnings / 333 files. Backend
> pytest still **805 passed** (no Python touched).
>
> The post-PR-88 housekeeping commit from earlier this session
> (`818cdfb` on `main`) is on origin now; the branch was
> created after it so the PR diff is clean.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session continued from /nextsession housekeeping into the
top-of-menu next piece: triaging the 12 Dependabot alerts on
`main`. Eleven of twelve are vite / esbuild dev-server
advisories in `gui/`; this PR bumps them to the patched
versions. The twelfth — a Linux-only Cargo transitive (`glib`
0.18.5) — is deferred as out of scope here (needs a tauri-stack
walk).

## What we shipped this session

### Commit `818cdfb` — post-PR-88 housekeeping (already on `origin/main`)

Single docs commit. Brought forward the two dangling
post-session handoffs (`2026-05-23T0755-utc-exists-semi-join-pr-86.md`
+ `2026-05-23T0907-utc-content-id-pr-88.md`), rewrote
NEXT_SESSION.md to reflect the post-merge state, and added
this session's housekeeping snapshot
(`2026-05-23T0956-utc-housekeeping-post-pr-88.md`).

### PR #89 — `chore(deps): bump vite ^6.4.2 + vitest ^3.2.4`

Branch: `chore/bump-vite-vitest-dependabot` (head `3f673bb`).
Single commit. No source under `gui/src/` touched.

| SHA | What |
|---|---|
| `3f673bb` | [gui/package.json](gui/package.json): `vite` `^6.0.0` → `^6.4.2`, `vitest` `^2.1.8` → `^3.2.4`. [gui/package-lock.json](gui/package-lock.json): regenerated; dual vite chain collapsed to one (net −882 lines). |

#### Acceptance — Dependabot alert criteria

| # | severity | package | first-patched | status |
|---|---|---|---|---|
| 12 | high | vite | 6.4.2 | ✅ cleared (vite → 6.4.2) |
| 11 | medium | vite | 6.4.1 | ✅ cleared |
| 10 | low | vite | 6.3.6 | ✅ cleared |
| 9 | low | vite | 6.3.6 | ✅ cleared |
| 8 | medium | vite | 6.1.6 | ✅ cleared |
| 7 | medium | vite | 6.0.15 | ✅ cleared |
| 6 | medium | vite | 6.0.14 | ✅ cleared |
| 5 | medium | vite | 6.0.13 | ✅ cleared |
| 4 | medium | vite | 6.0.12 | ✅ cleared |
| 2 | medium | vite | 6.4.2 | ✅ cleared |
| 1 | medium | esbuild | 0.25.0 | ✅ cleared (transitive → 0.25.12 via vitest 3) |
| 3 | medium | glib (Cargo) | 0.20.0 | ⏸️ deferred (Linux-only Tauri transitive) |

#### Test deltas

```
gui: npm audit:    12 → 0 vulnerabilities
gui: npm run build: ✅ vite v6.4.2 in 793ms (dist sizes unchanged)
gui: npm test:     271 passed / 271 (unchanged)
gui: npm run check: 0 errors, 0 warnings, 333 files
backend pytest:    805 passed (unchanged — no Python touched)
mypy:              4 pre-existing parser.py errors (unchanged)
```

### Docs updates this session

- **README.md** — unchanged (no user-visible behaviour change).
- **ROADMAP.md** — file does not exist in this repo; no update needed.
- **CLAUDE.md** — unchanged (no new invariant introduced; vite
  6.x → 6.x major patch + vitest 2.x → 3.x is fully transparent
  to the codebase).
- **NEXT_SESSION.md** — rewritten twice this session: once at
  housekeeping mid-point (saved as
  `docs/handoffs/2026-05-23T0956-utc-housekeeping-post-pr-88.md`),
  and now at session end with PR #89 details (this file).

## What's next

### 1. Merge PR #89

Low-risk dependency bump, no source touched, all checks green:

```bash
git checkout main
git pull
git branch -d chore/bump-vite-vitest-dependabot
# origin branch auto-deleted on merge.
```

### 2. Pick the next piece

In rough order of recommendation:

- **glib 0.18.5 Cargo alert (last remaining Dependabot item).**
  Linux-only transitive via the Tauri/webkit2gtk stack. `glib`
  is not in `gui/src-tauri/Cargo.toml`; need to identify which
  direct dep (likely `tauri` or `tauri-plugin-shell`) pulls it
  in and whether a feasible bump exists. Possibilities:
  (a) `cargo update -p glib --precise 0.20.0` if cargo can
  resolve it on its own; (b) bump the parent Tauri version to
  one whose lockfile resolves glib ≥ 0.20.0; (c) `cargo audit`
  to confirm the call sites in our code never actually hit
  `VariantStrIter`. Worth a focused session.
- **#38** `/v1/changes` semantics decision — once GUI client
  telemetry exists for the initial-load endpoint, pick one of
  (1) keep tail-subscription, point clients at `/v1/messages`
  for backfill; (2) add `min_id` for backward sweep; (3) keep
  `/v1/changes` strictly tail.
- **#87** CI-gated at-scale regression coverage for the
  folder-filter plan family. Infra-heavy; needs a strategy
  decision on where the harness runs.
- **`grow_pool` deep-pagination duplicates on `sort=rank`** —
  carried from PR #70 handoff. Revisit if rank-paginated
  duplicates become user-visible.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#5 / #4 / #2** Search-perf follow-ups.
- **#25** `websockets.legacy` DeprecationWarning — blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (deferred follow-up to
  #36). Gated on real ops data.

## Open decisions & risks

1. **vitest 2 → 3 major bump.** This is the only non-conservative
   move in PR #89. Mitigations:
   - The existing config (`vite.config.ts`: `test: {
     environment: "jsdom", globals: true }`) is the minimal
     vitest surface and works unchanged.
   - All 271 existing tests pass.
   - The bump is **load-bearing** for clearing the esbuild
     alert: vitest 2.x pinned a separate `vite@5 +
     esbuild@0.21.5` chain via its `vite-node` internals.
     Staying on vitest 2 would leave esbuild 0.24.2 in the
     install and the alert unresolved.

2. **glib 0.18.5 Cargo alert — DEFERRED.** Linux-only
   transitive; soundness bug in `Iterator` and
   `DoubleEndedIterator` impls for `glib::VariantStrIter`.
   The Tauri/webkit2gtk code path may or may not exercise
   `VariantStrIter` in practice. Filed in this session's
   "what's next" with three remediation paths to evaluate.

3. **Tauri Rust side not exercised this PR.** `gui/src-tauri/`
   is untouched and `cargo` was never invoked. The bump only
   affects the npm side (vite dev server, vitest harness,
   build output). Reviewer can spot-check `cargo tauri dev` /
   `cargo tauri build` on their workstation if desired; CI
   for the Tauri side is gated on #18 (no GUI CI yet).

4. **Carried forward from prior sessions (still load-bearing):**
   - **PR #88** (#10/#12 — `content_id` e2e coverage +
     docstring refresh) — full cid-rewrite chain is mutually
     load-bearing.
   - **PR #86** (folder-filter EXISTS semi-join, #85) —
     `build_where(folder_ids=…)` emits `WHERE EXISTS (SELECT 1
     FROM message_labels …)`. Do NOT re-introduce `SELECT
     DISTINCT` + `JOIN message_labels`.
   - **PR #84** (`PR-73 follow-up`) — `TrustedProxies`
     canonical in `src/localmail/api/client_ip.py`.
   - **PR #83** (`#79`) — `_mid_cursor_from_seed(cfg)` pure;
     PG≤17 / PG≥18 actual-rows parse.
   - **PR #82** (`#78`) — eligibility tests cover semi-join
     SQL shape.
   - **PR #81** (`#77`) — `BROWSE_ROW_SQL_TEMPLATE` +
     `compose_browse_sql` + `build_where` are the only
     authoritative browse SQL emitter.
   - **PR #80** (`#75`) — row-comparison keyset + NULL-tail
     top-up; do NOT rewrite the dated cursor predicate to the
     OR form.
   - **PR #76** — `messages_recent_idx` planner choice
     verified.
   - **PR #74** — `Searcher.get_pool_metadata` /
     `Searcher.config` public boundaries.
   - `auth.trusted_proxies` (#73), Postgres-backed login rate
     limiter (#7, PR #69), PR #70 (`sort=date` keyset +
     reranker off-by-default), MIME clamp list (#32),
     `parse_int_id` (#33), `rrf_k=60` (#35).

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin

# If PR #89 is still open:
git checkout chore/bump-vite-vitest-dependabot
gh pr view 89                              # check CI + review state

# After PR #89 is merged:
git checkout main
git pull
git branch -d chore/bump-vite-vitest-dependabot

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                         # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail    # 4 pre-existing parser.py errors

# GUI sanity:
cd gui && npm install && npm audit         # expect 0 vulnerabilities
npm test                                   # expect 271 passed
npm run build                              # expect clean vite v6.4.2
npm run check                              # expect 0 errors / 0 warnings
cd ..

# Pick next piece:
gh issue list --state open --limit 40

# If picking the glib Cargo alert:
cd gui/src-tauri
cargo tree -p glib                         # which parent pulls it?
cargo update -p glib --precise 0.20.0      # may fail if constrained
# Otherwise: bump the parent Tauri direct dep that pulls glib in.
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have
  a stale `VIRTUAL_ENV` pointing at some other pyenv venv.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0019 are additive.** Next migration would
  be `0020_*.sql`.
- **Wire `date` = `COALESCE(internal_date, date_sent)`** on
  every paginated list endpoint (PR #70).
- **Two cursor flavours on `/v1/search`** — `"<token>:<page>"`
  (pool) and `"K|<base64>"` (keyset, `sort=date` + non-empty
  query).
- **Page-cache miss → HTTP 409 `/problems/search-cursor-expired`**,
  never 500.
- **`reranker_enabled` default = False.** CPU-bound cross-encoder
  rerank fanout overruns timeouts when `grow_pool` doubles the
  pool.
- **`auth.trusted_proxies`** must contain the proxy's CIDR for
  the per-IP login cap. Do NOT also set `uvicorn
  --forwarded-allow-ips`.
- **`TrustedProxies` alias is canonical in
  `src/localmail/api/client_ip.py`** (PR #84). Do NOT
  re-introduce a local alias definition in `config.py`.
- **Probe-then-condition boundary** (#62) — for any new
  conditional-GET endpoint, the order is **ACL+probe →
  precondition → expensive IO**.
- **Streaming WARNING contract** (#58) — short-read detection
  via `_log_truncation()`.
- **ID-typing boundary** (#33) — routes accept `str`, cast via
  `localmail.api.ids.parse_int_id(...)`.
- **`Searcher` public boundaries** (PR #74) — use
  `searcher.get_pool_metadata(token, *, user_id)` and
  `searcher.config`, never `_cache` / `_cfg`.
- **`messages_recent_idx` planner choice** (#72, PR #76).
- **Dated-cursor predicate MUST use ROW comparison** (#75, PR
  #80).
- **NULL-tail top-up is conditional** (#75, PR #80).
- **Canonical browse SQL emitter** (#77, simplified by #85) —
  `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql(where=…)` +
  `build_where` in
  [src/localmail/api/browse.py](src/localmail/api/browse.py).
- **Folder-filter uses EXISTS semi-join** (#85, PR #86).
- **Folder-filter eligibility tests at fixture scale tolerate
  Sort nodes** (#85, PR #86).
- **content_id chain is end-to-end covered** (PR #88) — the
  full chain (`Attachment.content_id` → `_content_id` parser
  helper → `content_id` JSONB key → `_build_cid_map` →
  `cid_to_sha=` argument to `sanitize_html`) is mutually
  load-bearing.
- **NEW from this session: vite 6.4.2 + vitest 3.2.4 in gui/**
  (PR #89). Single deduped vite + esbuild chain in
  `package-lock.json`. Do NOT roll vitest back to 2.x — it
  re-introduces the `vite@5 + esbuild@0.21.5` shadow chain
  and re-opens the alert family.

## File map (as of branch HEAD `3f673bb`)

```
src/localmail/                              # unchanged this session
  api/messages.py                          # unchanged (post-PR #88)
  api/browse.py                            # unchanged (post-PR #86)
  config.py                                # unchanged (post-PR #84)
  parser.py / attachments.py / sanitize.py # unchanged
  search/ / serve/                         # unchanged
  cli.py / daemon.py / worker.py / ...     # unchanged
migrations/                                # 0001 … 0019_api_login_attempts.sql (unchanged)
tests/                                     # 805 passing (unchanged)
gui/
  package.json                             # MODIFIED (PR #89):
                                            # - vite ^6.0.0 → ^6.4.2
                                            # - vitest ^2.1.8 → ^3.2.4
  package-lock.json                        # MODIFIED (PR #89):
                                            # - regenerated; dual vite chain
                                            #   collapsed to one
                                            # - net −882 lines
  src/ / src-tauri/ / vite.config.ts / ... # unchanged
docs/handoffs/
  2026-05-23T1032-utc-vite-vitest-bump-pr-89.md   # THIS session's snapshot
  2026-05-23T0956-utc-housekeeping-post-pr-88.md  # earlier this session
  2026-05-23T0907-utc-content-id-pr-88.md         # prior
  2026-05-23T0755-utc-exists-semi-join-pr-86.md   # prior
  2026-05-23T0308-utc-pr73-followup-pr-84.md      # prior
  2026-05-22T0942-utc-harness-cleanup-pr-83.md    # prior
  2026-05-22T0721-utc-folder-filter-pr-82.md      # prior
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # prior
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                            # this file (post-session)
```

End of `chore/bump-vite-vitest-dependabot` session. PR #89 open
against `main` (`3f673bb`), clears 11/12 Dependabot alerts.
Branch alive on local + remote until merge. Next: merge #89,
then tackle the residual glib 0.18.5 Cargo alert (Linux-only
Tauri transitive) or pick from the carried menu — #38
(`/v1/changes` semantics), #87 (at-scale CI for folder-filter
plan), etc.
