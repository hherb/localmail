# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-24T2312 UTC (post-session).** PR **#94**
> (`refactor(gui): split AuthError::Io into AttachmentError + RawMessageError`)
> **open against `main`** on branch
> `refactor/gui-split-attachment-error` (head `76b70d9`). Single
> commit, 7 files changed (+387 / −95). New per-domain typed errors
> for the attachment-download and raw-message-download command
> families; `AuthError::Io` removed.
>
> CI on PR #94: **both `gui-ci` jobs SUCCESS** —
> `svelte-check + vitest` ✅ (completed 2026-05-24T23:12:15 UTC) and
> `cargo test + clippy` ✅ (completed 2026-05-24T23:13:20 UTC).
> `mergeable: MERGEABLE`. Local sanity: cargo
> **79/79 pass** under `cargo test --locked` (was 75; +4 new);
> `cargo clippy --locked -- -D warnings` clean; `npm run check`
> 0/0/333; `npm test` **276/276 pass** (was 271; +5 new tests
> in `format_error.test.ts`). Backend pytest **deliberately not
> re-run** — zero Python touched.
>
> **Issue closed**: **#22** (split `AuthError::Io` into a dedicated
> `AttachmentError`; option 1 from the issue body — per-domain enum).
> **Open issue count: 11 → 10** once PR #94 merges and the
> `Closes #22` trailer in the PR body fires.
>
> **Prior session (already merged)**: PR **#93**
> (`refactor(gui): rename change_poller.ts to change_helpers.ts`)
> merged as `06121c6`; closure #27.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session is the immediate follow-on to PR #93. With main clean,
I picked **#22** (the #1 recommended-next item in the prior handoff)
— biggest correctness win on the GUI side, fully doable headless
because every leg is `cargo test` + `vitest`.

## What we shipped this session

### PR #94 — `refactor(gui): split AuthError::Io into AttachmentError + RawMessageError (#22)`

Branch: `refactor/gui-split-attachment-error` (head `76b70d9`).
Single commit. **Closes #22.**

| SHA | What |
|---|---|
| `76b70d9` | New `AttachmentError` in [gui/src-tauri/src/commands/attachments.rs](gui/src-tauri/src/commands/attachments.rs) with typed variants `Auth (#[from] AuthError)`, `Setup (#[from] HttpError)`, `InvalidSha256`, `TooLarge { size, max }`, `Network`, `Http(u16)`, `Read`, `Write { path, error }`. New parallel `RawMessageError` in [gui/src-tauri/src/commands/raw_message.rs](gui/src-tauri/src/commands/raw_message.rs) covering the same migration on the raw-message download path (variant set is a subset: no `InvalidSha256`/`Write` since it uses `message_id` and returns bytes). `AuthError::Io` removed from [gui/src-tauri/src/commands/auth.rs](gui/src-tauri/src/commands/auth.rs). Stale doc-comment in [gui/src-tauri/src/commands/auth_change_password.rs](gui/src-tauri/src/commands/auth_change_password.rs) updated. JS-side wire-shape coverage added to [gui/src/lib/format_error.test.ts](gui/src/lib/format_error.test.ts) for `InvalidSha256` / `TooLarge` / `Http` / nested `Auth` wrapper / `Write`; [gui/src/lib/format_error.ts](gui/src/lib/format_error.ts) docstring lists the new wrappers. [gui/src/screens/settings/SettingsServer.test.ts](gui/src/screens/settings/SettingsServer.test.ts) transient-error mock updated from the now-impossible `{kind:"Io"}` to a realistic nested `{kind:"Http", detail:{kind:"Network", detail:"timeout"}}`. |

#### Why option 1 (per-domain enum) over option 2 (shared `CommandError` wrapper)

The issue offers two paths. Option 1 — per-domain enum — keeps the
auth-domain wrapping intact (only the cross-domain leakage moves out)
and is the smaller, more incremental change. Option 2 would have
introduced a top-level `CommandError` covering Auth/Attachment/Search
sub-domains and touched every `#[tauri::command]` return type for a
presentational improvement. The issue itself says "either is fine —
option 1 is smaller and more incremental." Option 1 also composes
cleanly with `#[from]`: `AttachmentError::Auth(#[from] AuthError)` +
`AttachmentError::Setup(#[from] HttpError)` mean `read_authenticated(...)?`
and `build_pinned_client(&pin)?` still propagate via `?` without
explicit `.map_err()` ceremony.

#### Why RawMessageError was bundled in (scope decision)

The issue body lists attachment-specific cases of `AuthError::Io`, but
"AuthError no longer has an Io variant" forces the parallel
`raw_message.rs` site to be migrated too — `raw_message.rs` built the
same network/HTTP/read/size-cap chain on `AuthError::Io`. Three options:

1. Leave `raw_message.rs` alone — can't, the `Io` variant is gone.
2. Re-route `raw_message.rs` through `AttachmentError` (semantically
   wrong: it's not an attachment) or invent a less-specific shared
   name.
3. **Per-domain enum for raw_message too** (chosen): separate command
   family, separate enum, same `#[from]` composition pattern. Matches
   the issue's stated principle ("per-domain enum") and the variant
   set is a subset of `AttachmentError`'s (no `InvalidSha256`, no
   `Write`).

The single-PR scope keeps the "no `Io` variant" invariant atomic — if
we'd split into two PRs, the intermediate state would leave
`raw_message.rs` either broken or hand-wrapping errors through
`AuthError` indirection.

#### Acceptance — verified locally

| Check | Result |
|---|---|
| `cargo test --locked` from `gui/src-tauri/` | ✅ 79/79 (was 75; +4 new) |
| `cargo clippy --locked -- -D warnings` | ✅ clean |
| `npm run check` (svelte-check) | ✅ 0 errors / 0 warnings / 333 files |
| `npm test` (vitest) | ✅ 276/276 (was 271; +5 new in `format_error.test.ts`) |
| `grep -rn "AuthError::Io\\|kind === ['\"]Io" gui/` | ✅ no hits (in TS/JS/Svelte/Rust) |
| PR #94 `gui-ci::svelte-check + vitest` | ✅ SUCCESS (2026-05-24T23:12:15 UTC) |
| PR #94 `gui-ci::cargo test + clippy` | ✅ SUCCESS (2026-05-24T23:13:20 UTC) |
| PR #94 `mergeable` | ✅ MERGEABLE |

#### Wire-shape invariants for AttachmentError / RawMessageError (load-bearing)

Both new enums serialise with `#[serde(tag = "kind", content = "detail")]`,
identical to `AuthError` / `HttpError`. The JS-side `formatError()`
walks `{kind, detail}` chains generically — no special-case logic
needed for the new types. Auth pre-check failures still surface as a
nested chain on the wire:

```
{kind: "Auth", detail: {kind: "NotConnected"}}
{kind: "Auth", detail: {kind: "NotLoggedIn"}}
{kind: "Auth", detail: {kind: "Http", detail: {kind: "HttpStatus", detail: {status, body}}}}
```

New variant payloads:

```
{kind: "InvalidSha256", detail: "<bad-input>"}
{kind: "TooLarge", detail: {size: u64, max: u64}}
{kind: "Network", detail: "<message>"}
{kind: "Http", detail: <u16>}          # NB: bare u16, NOT nested
{kind: "Read", detail: "<message>"}
{kind: "Write", detail: {path, error}} # attachment only
{kind: "Setup", detail: <HttpError>}   # pinned-client / URL parse
```

Tests in [gui/src-tauri/src/commands/attachments.rs](gui/src-tauri/src/commands/attachments.rs)
(`attachment_error_serializes_with_kind_and_detail_tags`,
`auth_pre_check_wraps_via_from_authentication`) and
[gui/src-tauri/src/commands/raw_message.rs](gui/src-tauri/src/commands/raw_message.rs)
(`raw_message_error_serializes_with_kind_and_detail_tags`) pin the
serialised form so any future enum rename surfaces as a Rust-side
test failure. JS-side coverage in
[gui/src/lib/format_error.test.ts](gui/src/lib/format_error.test.ts)
under the `describe("AttachmentError (issue #22)")` block mirrors the
same shapes from the consumer side.

### Docs updates this session

- **README.md** — unchanged (`grep AuthError README.md` is empty;
  the error types are internal to `gui/src-tauri/` and never named
  in user-facing docs).
- **ROADMAP.md** — does not exist in this repo; no update needed.
- **CLAUDE.md** — unchanged (no new project-level invariant; the
  per-domain enum pattern is internal to the GUI client and lives
  in the in-source docstrings + this NEXT_SESSION snapshot).
- **NEXT_SESSION.md** — rewritten this session-end (this file).
  Archived to `docs/handoffs/2026-05-24T2312-utc-attachment-error-pr-94.md`.

## What's next

### 1. Merge PR #94 once CI is green

Test-only changes plus type refactor; rust tests already cover wire
shape + variant pinning. Verify before merging:

```bash
gh pr view 94                              # confirm both checks SUCCESS + mergeable
gh pr merge 94 --squash                    # or squash via UI
git checkout main && git pull
git branch -d refactor/gui-split-attachment-error
```

After merge, open-issue count drops to **10**.

### 2. Pick the next piece

Remaining open issues (in rough order; top items are the same as
the previous handoff minus #22 just shipped):

- **#91** smoke-test Tauri dev/build after vite 6.4 + esbuild 0.25.
  **Now strongly recommended** because PR #94 changes Rust-side
  error types that surface through the IPC boundary — manual
  download-attachment + view-raw-message smoke confirms the
  serialised shape works end-to-end (the unit tests pin the JSON
  but only an actual run validates the Tauri invoke wrapper).
  Workstation-only verification (`cargo tauri dev` + `cargo tauri
  build` and a quick UI poke). Cannot be done headless in a
  background session — needs your hands on the keyboard with a
  display. **Acceptance**: dev launches without panics; build
  produces a working `.app`/`.deb`/`.msi`; manual smoke of one
  search round-trip + one attachment download + one raw-message
  view passes.
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

1. **PR #94 is fully green at session end** (`MERGEABLE`,
   both gui-ci jobs SUCCESS). Pure type refactor with extensive
   wire-shape coverage on both Rust and TS sides; no semantic
   risk to existing flows. Merge button is the only thing
   between this fix and `main`.

2. **Wire-shape compatibility risk: low.** Both new error types use
   the same `#[serde(tag = "kind", content = "detail")]` envelope as
   `AuthError`/`HttpError`. The JS-side `formatError()` walker is
   variant-agnostic. Only consumers that explicitly branched on
   `kind === "Io"` would break — `grep` confirms there are zero
   such branches in production code (only `SettingsServer.test.ts`
   had an "Io" string in a mock fixture, and that was updated to
   a realistic nested shape).

3. **AttachmentError vs RawMessageError variant duplication.** The
   two enums share ~6 of 8 / 6 of 6 variants. I considered extracting
   a shared `BlobDownloadError` but rejected it: (a) the issue
   explicitly asks for per-domain enums; (b) `Write` only makes
   sense on the attachment path; (c) future divergence (e.g. an
   `AttachmentError::PreviewExtractFailed`) would force a split
   back. Two small enums are clearer than one variant-bloated one.
   If a third blob-download command surfaces later, revisit.

4. **Carried forward from prior sessions (still load-bearing):**
   - **PR #93** (`refactor/gui-rename-change-helpers`, merged
     `06121c6`) — pure-helper modules under `gui/src/lib/` follow
     the `*_helpers.ts` suffix; `setInterval` loop stays in
     `stores/mail.svelte.ts::startPolling`.
   - **PR #92** (`fix/gui-format-test-tz-fragility`, merged
     `048cece`) — `formatRelativeDate` uses LOCAL-time `sameDay`.
   - **PR #89** (`vite ^6.4.2 + vitest ^3.2.4` — closed 11/12
     Dependabot alerts; residual is #90).
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

# If PR #94 is still open:
git checkout refactor/gui-split-attachment-error
gh pr view 94                              # confirm CI + mergeable

# After PR #94 is merged:
git checkout main
git pull
git branch -d refactor/gui-split-attachment-error

# Backend sanity (untouched this session; baseline carries):
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                         # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail    # 4 pre-existing parser.py errors

# GUI sanity (refactor verification):
cd gui/src-tauri && cargo test --locked    # expect 79 passed (was 75)
cd gui/src-tauri && cargo clippy --locked -- -D warnings   # expect clean
cd gui && npm run check                    # expect 0 errors / 0 warnings
cd gui && npm test                         # expect 276 passed (was 271)

# Pick next piece:
gh issue list --state open --limit 40

# If picking #91 (Tauri dev/build smoke — recommended next):
#   cd gui/src-tauri && cargo tauri dev
#   # confirm dev window launches, no panic banner in terminal
#   # in dev: open settings → confirm Re-trust cert works
#   # log in, browse to a message with an attachment, download it
#   # open the raw view of any message → confirm bytes load
#   cd gui/src-tauri && cargo tauri build --release
#   # confirm bundle path printed; double-click to launch
#
# If picking #24 (macOS to gui-ci matrix):
#   git checkout -b ci/gui-add-macos-matrix
#   # add strategy.matrix.os = [ubuntu-latest, macos-latest] to .github/workflows/gui-ci.yml
#   # run-on a draft PR first to confirm macOS minutes budget impact
#
# If picking #38 (/v1/changes semantics decision):
#   # Discussion + design, not pure code. Read issue body + linked
#   # design doc; pick from the three options outlined.
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
- **Pure-helper modules under `gui/src/lib/` follow the
  `*_helpers.ts` suffix** (PR #93). `change_helpers.ts` /
  `format_error.ts`. The `setInterval` loop that polls
  `/v1/changes` lives in `stores/mail.svelte.ts::startPolling`.
- **NEW from this session: per-domain typed errors for GUI
  blob-download commands** (PR #94). `AuthError` no longer carries
  a generic `Io(String)` variant; attachment and raw-message
  command families have their own enums
  (`AttachmentError`, `RawMessageError`) that compose `AuthError`
  via `#[from]`. Wire shape is `{kind, detail}` envelopes
  identical to `AuthError`/`HttpError`, so the JS-side
  `formatError()` works unchanged. If you add a new command
  family that downloads bytes (preview render, future MCP-style
  ops), give it a sibling enum rather than reaching for a
  catch-all "Io" — that conflation is exactly what #22 fixed.
  Tests pin the serialised shape on both sides:
  Rust `attachment_error_serializes_with_kind_and_detail_tags` /
  `raw_message_error_serializes_with_kind_and_detail_tags`,
  TS `format_error.test.ts::describe("AttachmentError (issue #22)")`.

## File map (as of branch HEAD `76b70d9`)

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
gui/src-tauri/src/commands/
  attachments.rs                            # MODIFIED (PR #94): + AttachmentError enum
                                            #   variants Auth, Setup, InvalidSha256,
                                            #   TooLarge, Network, Http, Read, Write
                                            #   + serialise + #[from] tests
  raw_message.rs                            # MODIFIED (PR #94): + RawMessageError enum
                                            #   subset (no InvalidSha256/Write)
                                            #   + serialise + #[from] tests
  auth.rs                                   # MODIFIED (PR #94): Io variant removed
  auth_change_password.rs                   # MODIFIED (PR #94): comment refresh
gui/src/lib/
  format_error.ts                          # MODIFIED (PR #94): docstring lists new wrappers
  format_error.test.ts                     # MODIFIED (PR #94): + AttachmentError describe block
                                            #   (InvalidSha256/TooLarge/Http/Auth/Write)
  change_helpers.ts / change_helpers.test.ts # unchanged (post-PR #93)
  stores/mail.svelte.ts                    # unchanged (post-PR #93)
gui/src/screens/settings/
  SettingsServer.test.ts                   # MODIFIED (PR #94): {kind:"Io"} mock → realistic
                                            #   nested {kind:"Http", detail:{kind:"Network"}}
docs/handoffs/
  2026-05-24T2312-utc-attachment-error-pr-94.md   # THIS session's snapshot
  2026-05-24T2251-utc-rename-change-helpers-pr-93.md  # prior
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

End of `refactor/gui-split-attachment-error` session. PR #94 open
against `main` (`76b70d9`); per-domain typed errors for the GUI
blob-download command families; **all four local gates green**
(cargo, clippy, svelte-check, vitest) — gui-ci confirmation needed
before merge. Closes #22. Open issue count 11 → 10 (post-merge).
Next: smoke #91 (Tauri dev/build manual verification —
recommended because PR #94 changes the IPC error envelope), then
#24 (macOS CI matrix) / #38 (changes-semantics decision).
