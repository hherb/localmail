# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-24T2251 UTC (post-session).** PR **#93**
> (`refactor(gui): rename change_poller.ts to change_helpers.ts`)
> **open against `main`** on branch
> `refactor/gui-rename-change-helpers` (head `4ad43e4`). Single
> commit, 4 files changed (+3 / −3): two `git mv` renames
> (`change_poller.{ts,test.ts}` → `change_helpers.{ts,test.ts}`) and
> three import-site updates. No source logic changed.
>
> CI on PR #93: **both `gui-ci` jobs SUCCESS** —
> `svelte-check + vitest` ✅ (completed 2026-05-24T22:50:58 UTC) and
> `cargo test + clippy` ✅ (completed 2026-05-24T22:51:50 UTC).
> `mergeable: MERGEABLE`. Local sanity: GUI test suite
> **271/271 pass** under AEST; `npm run check` clean
> (0 errors / 0 warnings / 333 files); the renamed file shows up
> as `src/lib/change_helpers.test.ts (7 tests)` in the vitest output.
> Backend pytest **deliberately not re-run** — zero Python touched.
>
> **Issue closed**: **#27** (cosmetic rename; option 1 from the
> issue body — keep helpers pure rather than fold `setInterval` back
> in). **Open issue count: 12 → 11** once PR #93 merges and the
> `Closes #27` trailer in the PR body fires.
>
> **Prior session (already merged)**: PR **#92** (TZ-fragile
> `formatRelativeDate` test fix) merged as `048cece`; closures
> #4, #17, #18 (commit `8e1e829` substantively addressed all
> three but never closed on GitHub); upstream-blocked analysis
> posted on #90.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session is the immediate follow-on to PR #92. With main clean,
I picked the smallest-surface item from the open-issue list (#27,
recommended top of the queue in the prior NEXT_SESSION).

## What we shipped this session

### PR #93 — `refactor(gui): rename change_poller.ts to change_helpers.ts (#27)`

Branch: `refactor/gui-rename-change-helpers` (head `4ad43e4`).
Single commit. No `gui/src-tauri/**` touched; no Python touched.
**Closes #27.**

| SHA | What |
|---|---|
| `4ad43e4` | `git mv gui/src/lib/change_poller.ts gui/src/lib/change_helpers.ts` + `git mv gui/src/lib/change_poller.test.ts gui/src/lib/change_helpers.test.ts`. Updated three import sites: [gui/src/lib/change_helpers.test.ts](gui/src/lib/change_helpers.test.ts), [gui/src/lib/stores/mail.svelte.ts](gui/src/lib/stores/mail.svelte.ts), [gui/src/lib/stores/mail.test.ts](gui/src/lib/stores/mail.test.ts). No behavioural change. |

#### Why option 1 (rename) over option 2 (fold `setInterval` in)

The issue offers two paths. Option 1 — rename the file — keeps the
exports as pure functions (`dedupNewMessages`, `parseCursor`) plus
a constant (`POLL_INTERVAL_MS`), so the vitest suite remains a
straight-line test of pure helpers (no fake timers, no store
fixtures, no Svelte runes). Option 2 would couple the helpers to
`stores/mail.svelte.ts` internals (the actual `setInterval` loop
lives in `startPolling`/`stopPolling`), enlarge the test surface,
and lose the clean module boundary. The issue itself says
"Either is fine; pick whichever fits the next person's mental
model best" — option 1 fits the established `gui/src/lib/*_helpers`
pattern (`format_error.ts`, etc.) and is reversible.

#### Acceptance — verified

| Check | Result |
|---|---|
| `npm run check` (svelte-check) | ✅ 0 errors / 0 warnings / 333 files |
| `npm test` (vitest, AEST local) | ✅ 271/271, incl. `change_helpers.test.ts` (7 tests) |
| `grep -rn "change_poller" gui/` (TS/JS/Svelte) | ✅ no hits |
| `git mv` preserves history (similarity 98% test, 100% source) | ✅ confirmed by `git diff --stat` |
| PR #93 `gui-ci::svelte-check + vitest` | ✅ SUCCESS (22:50:58 UTC) |
| PR #93 `gui-ci::cargo test + clippy` | ✅ SUCCESS (22:51:50 UTC) |
| PR #93 `mergeable` | ✅ MERGEABLE |

#### Why archives weren't touched

`grep -rn change_poller /Users/hherb/src/localmail --include="*.md"`
turns up references in
[docs/superpowers/plans/2026-05-18-localmail-gui-client-5-polish-packaging.md](docs/superpowers/plans/2026-05-18-localmail-gui-client-5-polish-packaging.md)
(the original implementation plan that introduced the file),
[docs/superpowers/plans/2026-05-20-pagination.md](docs/superpowers/plans/2026-05-20-pagination.md),
and prior `docs/handoffs/*.md` snapshots. These are **frozen
archive documents** — the handoff convention is that they capture
the state at a point in time; rewriting them silently would
destroy audit value. Leaving them alone is correct.

### Docs updates this session

- **README.md** — unchanged (`grep change_poller README.md` is
  empty; the file is internal to `gui/src/lib/` and never named
  in the user-facing docs).
- **ROADMAP.md** — does not exist in this repo; no update needed.
- **CLAUDE.md** — unchanged (no new invariant; the helper module
  has no role in the project-level guidance).
- **NEXT_SESSION.md** — rewritten this session-end with PR #93
  details (this file). Archived to
  `docs/handoffs/2026-05-24T2251-utc-rename-change-helpers-pr-93.md`.

## What's next

### 1. Merge PR #93 once you're satisfied

Test-only / rename-only. Both gui-ci jobs already green and
`mergeable: MERGEABLE` at session end:

```bash
gh pr view 93                              # confirm both checks SUCCESS
gh pr merge 93 --squash                    # or squash via UI
git checkout main && git pull
git branch -d refactor/gui-rename-change-helpers
```

After merge, open-issue count drops to **11**.

### 2. Pick the next piece

Remaining open issues (in rough order of recommendation; the
top three are the same as the previous handoff minus #27 just
shipped):

- **#22** split `AuthError::Io` into a dedicated `AttachmentError`
  in `gui/src-tauri/src/commands/`. Rust refactor; testable with
  `cargo test --locked` from macOS; touches every Tauri command
  return type that currently leaks `Io`. Real correctness win
  (the auth and attachment error families currently bleed into
  one another). **Acceptance** (from the issue body):
  - `AuthError` no longer has an `Io` variant.
  - Attachment commands return their own error type
    (`AttachmentError` with `InvalidSha256`, `TooLarge`, `Network`,
    `Http(StatusCode)`, `Read`, `Write` variants; option 1 from
    the issue).
  - All call sites (`gui/src/lib/tauri.ts` invoke wrappers + Svelte
    store `formatError()` helpers) handle the new type without a
    special-case for `Io`.
  - `cargo test --locked && cargo clippy --locked -- -D warnings`
    green; `npm run check` clean; `npm test` 271/271.
- **#91** smoke-test Tauri dev/build after vite 6.4 + esbuild 0.25.
  Workstation-only verification (`cargo tauri dev` + `cargo tauri
  build` and a quick UI poke). Cannot be done headless in a
  background session — needs your hands on the keyboard with a
  display. **Acceptance**: dev launches without panics; build
  produces a working `.app`/`.deb`/`.msi`; manual smoke of one
  search round-trip + one attachment download passes.
- **#38** `/v1/changes` semantics decision — needs GUI client
  telemetry to pick between (1) tail-subscription + `/v1/messages`
  backfill, (2) `min_id` for backward sweep, (3) strict tail.
  Decision, not pure code.
- **#28** GUI charset toggle / detection for `RawBodyView`. Needs
  Tauri dev to verify properly; bundle with #91 if pursued.
- **#24** add macOS to `gui-ci.yml` OS matrix. Touches CI only;
  needs careful budget management (macOS minutes are 10× pricier).
  **Acceptance**: workflow runs `ubuntu-latest` + `macos-latest`
  in a strategy matrix; both report SUCCESS on a green PR.
- **#87** CI-gated at-scale regression coverage for the
  folder-filter plan family. Infra-heavy; needs a strategy
  decision on where the harness runs.
- **#90** glib Cargo alert — **upstream-blocked**. Reopen action
  only when Tauri ships a release with gtk-rs ≥ 0.19 / glib ≥ 0.20.
- **#5 / #2** Search-perf follow-ups — explicitly deferred in the
  issues themselves until the large-archive / live-upgrade
  scenarios materialise.
- **#25** websockets.legacy DeprecationWarning — blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (follow-up to #36). Gated
  on real ops data.

## Open decisions & risks

1. **PR #93 is fully green at session end** (`MERGEABLE`,
   both gui-ci jobs SUCCESS). Pure rename; no semantic risk.
   Merge button is the only thing between this fix and `main`.

2. **Rename pattern**: future pure-helper modules under
   `gui/src/lib/` should use the `*_helpers.ts` suffix
   (matches `format_error.ts`, now `change_helpers.ts`).
   If the next person reaches for `change_poller.ts` in
   muscle memory, the file is gone — the import error
   guides them to the new name.

3. **Carried forward from prior sessions (still load-bearing):**
   - **PR #92** (`fix/gui-format-test-tz-fragility`, merged
     `048cece`) — `formatRelativeDate` uses LOCAL-time
     `sameDay`. Don't switch to UTC; if a future test fails
     the same way, the test data is wrong (must be
     local-day-anchored), not the source.
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

# If PR #93 is still open:
git checkout refactor/gui-rename-change-helpers
gh pr view 93                              # confirm CI + mergeable

# After PR #93 is merged:
git checkout main
git pull
git branch -d refactor/gui-rename-change-helpers

# Backend sanity (untouched this session; baseline carries):
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                         # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail    # 4 pre-existing parser.py errors

# GUI sanity (rename verification):
cd gui
npm test                                   # expect 271 passed (change_helpers.test.ts 7 tests)
npm run check                              # expect 0 errors / 0 warnings
cd ..

# Pick next piece:
gh issue list --state open --limit 40

# If picking #22 (split AuthError::Io → AttachmentError):
#   git checkout -b refactor/gui-split-attachment-error
#   cd gui/src-tauri
#   cargo test --locked                    # baseline
#   # add AttachmentError in src/commands/attachments.rs
#   # variants: InvalidSha256, TooLarge, Network(reqwest::Error),
#   #   Http(StatusCode), Read(reqwest::Error), Write(std::io::Error)
#   # bubble AuthError via #[from] so the auth pre-checks still compose
#   # update return types on attachment command(s)
#   # update gui/src/lib/tauri.ts invoke wrappers + Svelte store formatError()
#   cargo test --locked && cargo clippy --locked -- -D warnings
#   cd .. && npm run check && npm test

# If picking #24 (macOS to gui-ci matrix):
#   git checkout -b ci/gui-add-macos-matrix
#   # add strategy.matrix.os = [ubuntu-latest, macos-latest] to .github/workflows/gui-ci.yml
#   # run-on a draft PR first to confirm macOS minutes budget impact
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
- **`formatRelativeDate` uses LOCAL-time `sameDay`** (PR #92).
  The inline comment in `gui/src/lib/format.test.ts` documents
  the intentional choice — desktop clients render "today" in
  the user's wall-clock. Don't switch `sameDay` to UTC; if a
  future test fails the same way, the test data is wrong (must
  be local-day-anchored), not the source.
- **NEW from this session: pure-helper modules under `gui/src/lib/`
  follow the `*_helpers.ts` suffix** (`format_error.ts`,
  `change_helpers.ts`). The `setInterval` loop that polls
  `/v1/changes` still lives in `stores/mail.svelte.ts::startPolling`
  — the helpers (`dedupNewMessages`, `parseCursor`,
  `POLL_INTERVAL_MS`) are separately importable and unit-tested
  in `change_helpers.test.ts`. Don't try to "fix the misleading
  name" by folding `setInterval` into the helpers module — PR #93
  explicitly rejected that direction (option 2 from #27) to keep
  the helpers pure and test-isolated.

## File map (as of branch HEAD `4ad43e4`)

```
src/localmail/                              # unchanged this session
  api/messages.py                          # unchanged (post-PR #88)
  api/browse.py                            # unchanged (post-PR #86)
  config.py                                # unchanged (post-PR #84)
  parser.py / attachments.py / sanitize.py # unchanged
  search/ / serve/                         # unchanged
  cli.py / daemon.py / worker.py / ...     # unchanged
migrations/                                # 0001 … 0019_api_login_attempts.sql (unchanged)
tests/                                     # 805 passing (unchanged this session; baseline)
gui/
  src/lib/
    change_helpers.ts                      # RENAMED FROM change_poller.ts (PR #93)
    change_helpers.test.ts                 # RENAMED FROM change_poller.test.ts (PR #93)
                                            # - import updated to "./change_helpers"
    stores/mail.svelte.ts                  # MODIFIED (PR #93): import "../change_helpers"
    stores/mail.test.ts                    # MODIFIED (PR #93): import "../change_helpers"
    format.ts / format.test.ts             # unchanged (post-PR #92)
  package.json / package-lock.json         # unchanged (post-PR #89)
  src-tauri/                               # unchanged
docs/handoffs/
  2026-05-24T2251-utc-rename-change-helpers-pr-93.md  # THIS session's snapshot
  2026-05-24T2236-utc-format-test-fix-pr-92.md        # prior
  2026-05-23T1032-utc-vite-vitest-bump-pr-89.md       # prior
  2026-05-23T0956-utc-housekeeping-post-pr-88.md      # prior
  2026-05-23T0907-utc-content-id-pr-88.md             # prior
  2026-05-23T0755-utc-exists-semi-join-pr-86.md       # prior
  2026-05-23T0308-utc-pr73-followup-pr-84.md          # prior
  2026-05-22T0942-utc-harness-cleanup-pr-83.md        # prior
  2026-05-22T0721-utc-folder-filter-pr-82.md          # prior
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # prior
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                            # this file (post-session)
```

End of `refactor/gui-rename-change-helpers` session. PR #93 open
against `main` (`4ad43e4`), pure rename of one helper module;
**both gui-ci jobs SUCCESS** — ready to merge. Closes #27. Open
issue count 12 → 11 (post-merge). Next: merge #93, then pick from
#22 (Rust error refactor — biggest correctness win) / #91 (Tauri
smoke on your workstation) / #24 (macOS CI matrix).
