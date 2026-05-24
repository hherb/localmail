# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-24T2236 UTC (post-session).** PR **#92**
> (`test(gui/format): fix TZ-fragile same-day formatRelativeDate
> assertion`) **open against `main`** on branch
> `fix/gui-format-test-tz-fragility` (head `a307685`). Single
> commit, +8 / −4 in one file (`gui/src/lib/format.test.ts`).
> Source code unchanged.
>
> CI on PR #92: **both `gui-ci` jobs green** —
> `svelte-check + vitest` ✅ and `cargo test + clippy` ✅
> (completed at 2026-05-24T22:35:52 UTC, mergeable now).
> Local sanity: GUI test suite **271/271 pass**
> under TZ=local (AEST), TZ=UTC, TZ=America/Los_Angeles, and
> TZ=Pacific/Kiritimati (UTC+14). Backend pytest still **805 passed**
> (no Python touched). `npm run check` clean (0 errors / 0 warnings
> / 333 files); `npm run build` unchanged from PR #89.
>
> **Issue housekeeping**: closed **#4**, **#17**, **#18** —
> already substantively addressed in commit `8e1e829` (2026-05-18)
> but never closed on GitHub; each closure carries a verification
> note pointing at the load-bearing files + tests. Posted
> upstream-blocked analysis on **#90** (glib unsoundness) — Tauri
> 2.11.2 is crates.io-latest, gtk-rs 0.18 ↔ glib 0.18 tight
> coupling rules out the three remediation paths originally
> proposed in the issue. **Open issue count: 15 → 12.**

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session continued from PR #89 (vite/vitest dependency bump,
merged as `9746fa2`). When verifying the post-merge baseline, the
local `npm test` exposed a flaky GUI assertion — same-day comparison
in `formatRelativeDate` uses local time but the test fed UTC
timestamps that span midnight in AEST. Fix is to the test only;
the source behaviour is correct.

## What we shipped this session

### PR #92 — `test(gui/format): fix TZ-fragile same-day formatRelativeDate assertion`

Branch: `fix/gui-format-test-tz-fragility` (head `a307685`).
Single commit. No source under `gui/src/lib/format.ts` touched.

| SHA | What |
|---|---|
| `a307685` | [gui/src/lib/format.test.ts](gui/src/lib/format.test.ts): rewrite the "returns time only for same day" assertion to build both inputs with `new Date(y, m, d, h, m)` so they share a local calendar day in any timezone. Inline comment documents the intentional local-time semantics in `formatRelativeDate`. |

#### Acceptance — verified TZ-independence

| TZ | `npm test` | Notes |
|---|---|---|
| AEST (local — UTC+10) | ✅ 271/271 | was 270/271 before; reproduced the original failure |
| UTC | ✅ 271/271 | |
| America/Los_Angeles (UTC-7/8) | ✅ 271/271 | |
| Pacific/Kiritimati (UTC+14, easternmost) | ✅ 271/271 | confirms the easternmost edge |

#### Why the source is unchanged

`formatRelativeDate` calls `sameDay()` which compares calendar days
in **local time** — this is correct for a desktop client. The user's
"today" is their wall-clock day; rendering a 23:50-local email
that arrived 10 minutes ago as "yesterday" because UTC ticked over
would be the actual bug. The test was UTC-naive; the source is fine.

### Issue housekeeping — closures and triage

- **Closed #4** (`search: embedding backend — explicit model paths
  + verified query/document task prefixes`). Verified done in commit
  `8e1e829`: [src/localmail/search/embeddings.py](src/localmail/search/embeddings.py)
  ships `_MODEL_PATH_REGISTRY`, `_resolve_model_path()` precedence
  (`embedding_model_path` override → registry → pass-through), and
  raises `EmbeddingConfigError` when fastembed lacks `query_embed`
  (no silent fallback to `embed`). 8 tests in
  [tests/test_embeddings.py](tests/test_embeddings.py) cover all
  three precedence paths plus the failure mode.
- **Closed #17** (`[gui] Tighten tsconfig once real surface lands`).
  [gui/tsconfig.json](gui/tsconfig.json) has `noUnusedLocals: true`
  + `noUnusedParameters: true`. `npm run check` clean.
- **Closed #18** (`[gui] Add CI workflow for the gui/ subproject`).
  [.github/workflows/gui-ci.yml](.github/workflows/gui-ci.yml) runs
  `svelte-check + vitest` (`frontend` job) and `cargo test
  --locked + cargo clippy --locked -- -D warnings` (`tauri-rust`
  job) on push to `main` and on PRs touching `gui/**`. macOS / Windows
  matrix is the deliberate follow-up #24.
- **Commented on #90** (`chore(deps): resolve glib unsoundness via
  Tauri stack bump`): walked the three proposed remediation paths,
  all blocked.
    - `cargo update -p glib --precise 0.20.0` violates `gtk ^0.18`
      SemVer requirement; cargo refuses.
    - Tauri 2.11.2 is **crates.io-latest** (verified with `cargo
      info tauri`); no newer 2.x exists. `wry 0.55.1` also latest.
      `tao 0.35.3` and `muda 0.19.2` are patch bumps that don't
      move off gtk-rs 0.18. The whole Tauri 2.x line still pins
      gtk-rs 0.18 → glib 0.18.
    - `[patch.crates-io]` of glib alone would break the gtk-rs
      0.18 binding crates (gtk/gdk/gio/etc) at compile time —
      0.20 API surface is incompatible. Patching the whole
      gtk-rs 0.18 stack to 0.20 = bumping Tauri itself, back to
      the previous path.
  - **Threat model**: Linux-only (macOS/Windows don't link glib).
    Zero `glib::VariantStrIter` / `glib::Variant` / `glib::*`
    references in [gui/src-tauri/src/](gui/src-tauri/src/) — the
    vulnerable iterator is unreachable from our code. Medium
    severity, no PoC.
  - **Recommendation**: defer until Tauri ships a release that
    bumps the gtk-rs stack. Then it's a one-line `tauri = "2.X"`
    bump. Acceptance gate: `cargo tree -p glib` shows ≥ 0.20.

### Docs updates this session

- **README.md** — unchanged (the test fix has no user-visible
  surface; the source behaviour was already correct).
- **ROADMAP.md** — does not exist in this repo; no update needed.
- **CLAUDE.md** — unchanged (no new invariant; the local-time
  `sameDay` semantics were already implicit in the GUI design).
- **NEXT_SESSION.md** — rewritten this session-end with PR #92
  details, closures #4/#17/#18, and the #90 upstream-blocked
  analysis (this file). Archived to
  `docs/handoffs/2026-05-24T2236-utc-format-test-fix-pr-92.md`.

## What's next

### 1. Merge PR #92 once CI green

Test-only change, no source touched. Once `gui-ci.yml::tauri-rust`
finishes:

```bash
gh pr view 92                              # confirm both checks SUCCESS
gh pr merge 92 --squash                    # or squash via UI
git checkout main && git pull
git branch -d fix/gui-format-test-tz-fragility
```

### 2. Pick the next piece

Open issue list now sits at 12 items. In rough order of recommendation:

- **#91** smoke-test Tauri dev/build after vite 6.4 + esbuild 0.25.
  Workstation-only verification (`cargo tauri dev` + `cargo tauri
  build` and a quick UI poke). Cannot be done headless in a
  background session — needs your hands on the keyboard with a
  display.
- **#27** rename `gui/src/lib/change_poller.ts` → `change_helpers.ts`
  (or fold `setInterval` back in). Cosmetic; small; ships in
  minutes. Pure helpers are 24 lines, tests are 7 — surface is
  trivial.
- **#22** split `AuthError::Io` into a dedicated `AttachmentError`
  in `gui/src-tauri/src/commands/`. Rust refactor; testable with
  `cargo test --locked` from macOS; touches every Tauri command
  return type that currently leaks `Io`. Real correctness win
  (the auth and attachment error families currently bleed into
  one another).
- **#38** `/v1/changes` semantics decision — needs GUI client
  telemetry to pick between (1) tail-subscription + `/v1/messages`
  backfill, (2) `min_id` for backward sweep, (3) strict tail.
  Decision, not pure code.
- **#28** GUI charset toggle / detection for `RawBodyView`. Needs
  Tauri dev to verify properly; bundle with #91 if pursued.
- **#24** add macOS to `gui-ci.yml` OS matrix. Touches CI only;
  needs careful budget management (macOS minutes are 10× pricier).
- **#87** CI-gated at-scale regression coverage for the
  folder-filter plan family. Infra-heavy; needs a strategy
  decision on where the harness runs.
- **#90** glib Cargo alert — **upstream-blocked**. Reopen action
  only when Tauri 2.X ships with gtk-rs ≥ 0.19.
- **#5 / #2** Search-perf follow-ups — explicitly deferred in the
  issues themselves until the large-archive / live-upgrade
  scenarios materialise.
- **#25** websockets.legacy DeprecationWarning — blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (follow-up to #36). Gated
  on real ops data.

## Open decisions & risks

1. **PR #92 CI is fully green at session end.** Both `frontend`
   and `tauri-rust` gui-ci jobs completed SUCCESS. The PR is
   mergeable now; the squash button is the only thing standing
   between this fix and `main`.

2. **Local-time `sameDay` is intentional and load-bearing.** Don't
   "fix" it to UTC later. The new inline comment in
   `format.test.ts` documents this; if anyone proposes the change,
   point them at the comment + this handoff.

3. **#90 glib stays open as a documented upstream blocker.** This
   is appropriate — the alert is a real medium-severity advisory
   and should not be silently closed. The threat-model comment
   on the issue is the operational signal that no in-repo work
   unlocks it. Re-evaluate when Tauri ships a gtk-rs bump.

4. **Carried forward from prior sessions (still load-bearing):**
   - **PR #89** (`vite ^6.4.2 + vitest ^3.2.4` — closed 11/12
     Dependabot alerts; the residual is #90).
   - **PR #88** (#10/#12 — `content_id` e2e coverage + docstring
     refresh) — full cid-rewrite chain is mutually load-bearing.
   - **PR #86** (folder-filter EXISTS semi-join, #85) —
     `build_where(folder_ids=…)` emits `WHERE EXISTS (SELECT 1
     FROM message_labels …)`. Do NOT re-introduce `SELECT
     DISTINCT` + `JOIN message_labels`.
   - **PR #84** (`PR-73 follow-up`) — `TrustedProxies` canonical
     in `src/localmail/api/client_ip.py`.
   - **PR #83** (`#79`) — `_mid_cursor_from_seed(cfg)` pure;
     PG≤17 / PG≥18 actual-rows parse.
   - **PR #82** (`#78`) — eligibility tests cover semi-join SQL
     shape.
   - **PR #81** (`#77`) — `BROWSE_ROW_SQL_TEMPLATE` +
     `compose_browse_sql` + `build_where` are the only
     authoritative browse SQL emitter.
   - **PR #80** (`#75`) — row-comparison keyset + NULL-tail
     top-up; do NOT rewrite the dated cursor predicate to the
     OR form.
   - **PR #76** — `messages_recent_idx` planner choice verified.
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

# If PR #92 is still open:
git checkout fix/gui-format-test-tz-fragility
gh pr view 92                              # check CI + review state

# After PR #92 is merged:
git checkout main
git pull
git branch -d fix/gui-format-test-tz-fragility

# Backend sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                         # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail    # 4 pre-existing parser.py errors

# GUI sanity (test fix verification):
cd gui
npm test                                   # expect 271 passed (was 270/271 on main pre-#92)
TZ=UTC npm test                            # expect 271 passed
TZ=Pacific/Kiritimati npm test             # expect 271 passed
npm run check                              # expect 0 errors / 0 warnings
npm run build                              # expect clean vite v6.4.2
cd ..

# Pick next piece:
gh issue list --state open --limit 40

# If picking #27 (rename change_poller.ts):
#   git checkout -b refactor/gui-rename-change-helpers
#   git mv gui/src/lib/change_poller.ts gui/src/lib/change_helpers.ts
#   git mv gui/src/lib/change_poller.test.ts gui/src/lib/change_helpers.test.ts
#   # update imports in gui/src/lib/stores/mail.svelte.ts and any other call sites
#   cd gui && npm run check && npm test

# If picking #22 (split AuthError::Io → AttachmentError):
#   cd gui/src-tauri
#   cargo test --locked                    # baseline
#   # add AttachmentError in src/commands/attachments.rs with #[from] AuthError
#   # update return types on the attachment command(s)
#   # update gui/src/lib/tauri.ts invoke wrappers + Svelte store formatError() helpers
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a
  stale `VIRTUAL_ENV` pointing at some other pyenv venv.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0019 are additive.** Next migration would be
  `0020_*.sql`.
- **Wire `date` = `COALESCE(internal_date, date_sent)`** on every
  paginated list endpoint (PR #70).
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
- **Streaming WARNING contract** (#58) — short-read detection via
  `_log_truncation()`.
- **ID-typing boundary** (#33) — routes accept `str`, cast via
  `localmail.api.ids.parse_int_id(...)`.
- **`Searcher` public boundaries** (PR #74) — use
  `searcher.get_pool_metadata(token, *, user_id)` and
  `searcher.config`, never `_cache` / `_cfg`.
- **`messages_recent_idx` planner choice** (#72, PR #76).
- **Dated-cursor predicate MUST use ROW comparison** (#75, PR #80).
- **NULL-tail top-up is conditional** (#75, PR #80).
- **Canonical browse SQL emitter** (#77, simplified by #85) —
  `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql(where=…)` +
  `build_where` in
  [src/localmail/api/browse.py](src/localmail/api/browse.py).
- **Folder-filter uses EXISTS semi-join** (#85, PR #86).
- **Folder-filter eligibility tests at fixture scale tolerate
  Sort nodes** (#85, PR #86).
- **content_id chain is end-to-end covered** (PR #88) — the full
  chain (`Attachment.content_id` → `_content_id` parser helper →
  `content_id` JSONB key → `_build_cid_map` → `cid_to_sha=`
  argument to `sanitize_html`) is mutually load-bearing.
- **vite 6.4.2 + vitest 3.2.4 in gui/** (PR #89). Single deduped
  vite + esbuild chain in `package-lock.json`. Do NOT roll vitest
  back to 2.x — it re-introduces the `vite@5 + esbuild@0.21.5`
  shadow chain and re-opens the alert family.
- **NEW from this session: `formatRelativeDate` uses LOCAL-time
  `sameDay`** (PR #92). The new inline comment in
  `gui/src/lib/format.test.ts` documents the intentional choice
  — desktop clients render "today" in the user's wall-clock.
  Don't switch `sameDay` to UTC; if a future test fails the
  same way, the test data is wrong (must be local-day-anchored),
  not the source.

## File map (as of branch HEAD `a307685`)

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
  src/lib/format.ts                        # unchanged
  src/lib/format.test.ts                   # MODIFIED (PR #92):
                                            # - "same day" assertion built
                                            #   with `new Date(y,m,d,h,m)`
                                            #   so the inputs share a local
                                            #   calendar day in any TZ
                                            # - inline comment documents
                                            #   the LOCAL-time semantics
  package.json / package-lock.json         # unchanged (post-PR #89)
  src/ / src-tauri/ / vite.config.ts / ... # unchanged
docs/handoffs/
  2026-05-24T2236-utc-format-test-fix-pr-92.md    # THIS session's snapshot
  2026-05-23T1032-utc-vite-vitest-bump-pr-89.md   # prior
  2026-05-23T0956-utc-housekeeping-post-pr-88.md  # prior
  2026-05-23T0907-utc-content-id-pr-88.md         # prior
  2026-05-23T0755-utc-exists-semi-join-pr-86.md   # prior
  2026-05-23T0308-utc-pr73-followup-pr-84.md      # prior
  2026-05-22T0942-utc-harness-cleanup-pr-83.md    # prior
  2026-05-22T0721-utc-folder-filter-pr-82.md      # prior
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # prior
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                            # this file (post-session)
```

End of `fix/gui-format-test-tz-fragility` session. PR #92 open
against `main` (`a307685`), 1-test bugfix; **both gui-ci jobs
SUCCESS** — ready to merge. Three stale issues closed (#4, #17,
#18); upstream-blocked analysis posted on #90. Open issue count
15 → 12. Next: merge #92, then pick from #27 (rename) / #22
(error refactor) / #91 (Tauri smoke on your workstation).
