# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-25T0522 UTC (post-session).**
> **PR #94 merged** as `ecf7e34` on `main` at 2026-05-25T05:18:40Z
> (`refactor(gui): split AuthError::Io into AttachmentError +
> RawMessageError (#22) (#94)`) — squash merge by maintainer between
> the prior session's wrap-up and this one. **Issue #22 closed**
> by the merge.
>
> Local branch `refactor/gui-split-attachment-error` deleted
> (`903dbb3`); `git status` clean on `main` (only untracked
> `.claude/settings.local.json`, a local IDE-permission file —
> *not* gitignored but also not part of this project's convention
> to commit; left alone).
>
> **No code touched this session** — pure housekeeping after the
> upstream merge. No tests run; nothing in `src/`, `gui/`,
> `migrations/`, or `tests/` changed since `ecf7e34`. Backend
> baseline (805 passing pytest, 4 pre-existing mypy errors in
> `parser.py`) and GUI baseline (cargo 79/79, clippy clean,
> svelte-check 0/0/333, vitest 276/276) all carry forward
> unchanged.
>
> **Open issue count: 10** (was 11 before #94 merged; #22 closed).
>
> **Why this is a housekeeping-only session**: the previous
> handoff named **#91** (Tauri dev/build smoke after the IPC
> envelope change) as the strongly-recommended next pick, but
> #91 cannot be done headless — it requires `cargo tauri dev` +
> `cargo tauri build` with the maintainer's hands on the keyboard
> and an attached display. Every other open issue either (a)
> needs user-side input/decision, (b) is upstream-blocked, or
> (c) was explicitly deferred in the issue body itself. Rather
> than manufacture work, this session confirms post-merge state
> and refreshes the handoff so the next interactive session can
> pick cleanly.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

**Nothing in the codebase.** PR #94 was authored + verified in the
prior session and merged externally by the maintainer. The only
artefacts of this session are:

- `git branch -d refactor/gui-split-attachment-error` (local
  cleanup of the merged branch; `903dbb3`).
- This file (`NEXT_SESSION.md`) refreshed to post-merge state.
- `docs/handoffs/2026-05-25T0522-utc-housekeeping-post-pr-94.md` —
  archived snapshot.

No commits authored this session. The HEAD of `main` remains
`ecf7e34`.

### Docs review

- **README.md** — checked, no AuthError mention; unchanged.
  PR #94 was an internal-only type refactor (GUI's Tauri command
  error envelopes) and never surfaced in user-facing docs.
- **ROADMAP.md** — does not exist in this repo; no update needed.
- **CLAUDE.md** — unchanged (PR #94's per-domain-enum pattern is
  captured in the in-source docstrings + the prior session's
  archived handoff; not a project-level invariant worth
  promoting to CLAUDE.md).

## What's next

Same ranking as the prior handoff, minus #22 which is now closed.

### 1. **#91 — smoke-test Tauri dev/build** *(needs human at keyboard)*

**Strongly recommended next** because PR #94 changed the
Rust→JS error envelope (`AttachmentError`, `RawMessageError`
replace the `AuthError::Io` indirection on the attachment-download
and raw-message-download paths). Unit tests pin the serialised
`{kind, detail}` JSON shape on both sides
(`attachment_error_serializes_with_kind_and_detail_tags`,
`raw_message_error_serializes_with_kind_and_detail_tags`,
`format_error.test.ts::describe("AttachmentError (issue #22)")`),
but only an actual Tauri run validates the IPC `invoke` wrapper
end-to-end.

**Acceptance**:
- `cargo tauri dev` launches without panics.
- `cargo tauri build --release` produces a working bundle
  (`.app` on macOS).
- Manual smoke pass: log in → list messages → download one
  attachment (verify the download dialog, not inline render) →
  open one raw-message view (verify bytes load, no error
  banner).
- *Negative path* (nice to have): force-fail one attachment
  download (delete the blob from disk, or hit it with an
  ACL-denied token) and confirm the error banner renders the
  `formatError()` output cleanly — no `{kind:undefined}` /
  `[object Object]` / similar wire-shape regressions.

If #91 is done, immediately bundle in **#28** (charset toggle /
detection for `RawBodyView`) — same human-at-keyboard verification
surface, marginal additional cost.

### 2. **#38 — `/v1/changes` semantics decision** *(needs user input)*

Three options outlined in the issue body:
1. Tail-subscription + `/v1/messages` backfill.
2. `min_id` for backward sweep.
3. Strict tail.

Needs telemetry from the GUI client + a design call between
those three; not a pure-code task. The issue cannot make
forward progress without the maintainer picking a direction.

### 3. **#24 — macOS to gui-ci.yml OS matrix** *(headless-doable; spends CI minutes)*

Concrete YAML change: convert the `tauri-rust` job to a
`strategy.matrix.os: [ubuntu-latest, macos-latest]`. The
cost concern (macOS minutes are 10× pricier) means the
maintainer should run this on a draft PR first to confirm
budget impact before merging.

**Acceptance**: gui-ci.yml runs both `ubuntu-latest` +
`macos-latest`; both report SUCCESS on a clean PR.

This is the most autonomous-friendly remaining item — but
adds a real cost and the CI run itself is the test (can't
validate the change without spending the minutes).

### 4. **#87 — CI-gated at-scale regression coverage for folder-filter** *(needs scale-tuning + decision)*

Follow-up to PR #86. Two options in the issue body:
1. Plan-signature assertion at smaller scale (assert no `Unique`
   node, no full-projection Sort on the broad-folder probe).
2. Buffer-hit ceiling test.

Issue itself says "pick whichever is least flaky against PG
version drift" — implies option 1 is preferred, but the
scale-tuning is empirical: the DISTINCT-regression signature
only surfaces at scales where the planner prefers the
date-ordered walk, which is PG-version + statistics dependent.
The current acceptance harness uses 200k rows; CI-friendly is
probably 20k–50k, but that needs an experimental sweep to find
the threshold. Substantial.

### Other open issues (blocked / deferred)

- **#90** glib Cargo alert — upstream-blocked. Action only when
  Tauri ships a release with gtk-rs ≥ 0.19 / glib ≥ 0.20.
- **#25** websockets.legacy DeprecationWarning — blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (follow-up to #36).
  Gated on real ops data.
- **#5** Search batch INSERT for chunking loop — issue itself
  says "Defer until someone actually measures backfill time on
  a large archive."
- **#2** Migration 0006 GIN CONCURRENT for live-upgrade — issue
  itself says "deferring until someone actually hits the
  live-upgrade scenario."

## Open decisions & risks

1. **Empty "what we shipped" is intentional.** PR #94 was
   shipped in the prior session and merged externally; nothing
   new was authored here. The handoff exists because the
   `/nextsession` flow always refreshes `NEXT_SESSION.md` and
   archives a snapshot — that's a documentation rhythm,
   independent of whether a session produced code.

2. **No headless-doable issue is genuinely ready to autonomously
   pick up.** Every remaining open issue needs one of:
   maintainer hands-on (Tauri dev for #91/#28), a design call
   (#38), CI-minute budget approval (#24), or empirical
   scale-tuning + a strategy decision (#87). Picking any of
   these without the user's involvement would either burn money
   on speculative CI runs (#24) or produce work that lands in
   the wrong design direction.

3. **Carried-forward invariants (still load-bearing)** — same
   list as the prior handoff; not duplicated here. See
   `docs/handoffs/2026-05-24T2312-utc-attachment-error-pr-94.md`
   "Carried forward from prior sessions" for the full enumeration
   (PRs #70–#94, every flag/cursor/SQL-shape invariant). Nothing
   in that list changed this session.

4. **Per-domain typed errors for GUI blob-download commands
   (PR #94, now on main):** `AuthError` no longer carries
   `Io(String)`. Attachment + raw-message command families have
   their own enums (`AttachmentError`, `RawMessageError`) that
   compose `AuthError` via `#[from]`. Wire shape is `{kind,
   detail}` envelopes identical to `AuthError`/`HttpError`. If
   a new command family that downloads bytes appears (preview
   render, future MCP-style ops), give it a sibling enum rather
   than a catch-all "Io" — that conflation is exactly what #22
   fixed.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin
git checkout main && git pull        # should already be on ecf7e34
git status                            # expect clean (.claude/settings.local.json untracked is fine)

# Backend sanity (untouched this session; baseline carries):
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                   # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail    # 4 pre-existing parser.py errors

# GUI sanity (PR #94 baseline, now on main):
cd gui/src-tauri && cargo test --locked    # expect 79 passed
cd gui/src-tauri && cargo clippy --locked -- -D warnings   # expect clean
cd gui && npm run check                    # expect 0 errors / 0 warnings / 333 files
cd gui && npm test                         # expect 276 passed

# Pick next piece:
gh issue list --state open --limit 40

# Recommended: #91 — smoke-test Tauri dev/build (needs you at the keyboard):
cd gui/src-tauri && cargo tauri dev
#   - confirm dev window launches, no panic banner in terminal
#   - log in via settings; confirm Re-trust cert works
#   - browse to a message with an attachment; download it
#     (confirm download dialog, NOT inline render — that's the
#     load-bearing #32 / #54 / #59 / #62 / #64 invariant)
#   - open a raw-message view (any message); confirm bytes load
#   - (negative path) try downloading a blob whose ACL grant
#     was revoked; confirm formatError() banner renders cleanly
cd gui/src-tauri && cargo tauri build --release
#   - confirm bundle path printed; double-click the .app to launch

# If picking #24 (macOS CI matrix — only autonomous-friendly remaining item):
git checkout -b ci/gui-add-macos-matrix
# edit .github/workflows/gui-ci.yml:
#   tauri-rust: add strategy.matrix.os: [ubuntu-latest, macos-latest]
#                add runs-on: ${{ matrix.os }}
#                add fail-fast: false
# push as draft PR first to confirm macOS minute consumption
# before un-drafting.

# If picking #38 (/v1/changes semantics):
#   Read issue #38; pick one of three options; design discussion.
```

## Known gotchas (still load-bearing)

Same enumeration as the prior handoff — nothing changed this
session. Quick reference (full detail in
`docs/handoffs/2026-05-24T2312-utc-attachment-error-pr-94.md`):

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a
  stale `VIRTUAL_ENV`.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live DB.
- **Next migration would be `0020_*.sql`** (latest is `0019`).
- **Wire `date` = `COALESCE(internal_date, date_sent)`** on every
  paginated list endpoint.
- **Two cursor flavours on `/v1/search`** — `"<token>:<page>"`
  (pool) and `"K|<base64>"` (keyset, `sort=date` + non-empty
  query).
- **Page-cache miss → HTTP 409**, never 500.
- **`reranker_enabled` default = False.**
- **`auth.trusted_proxies`** must contain the proxy's CIDR for
  the per-IP login cap. Do NOT also set `uvicorn
  --forwarded-allow-ips`.
- **Probe-then-condition boundary** (#62) — ACL+probe →
  precondition → expensive IO.
- **Streaming WARNING contract** (#58) — `_log_truncation()`.
- **ID-typing boundary** (#33) — `parse_int_id(...)`.
- **`Searcher` public boundaries** (PR #74) —
  `get_pool_metadata`, `config`.
- **`messages_recent_idx` planner choice** (#72, PR #76).
- **Dated-cursor predicate uses ROW comparison** (#75, PR #80).
- **NULL-tail top-up is conditional** (#75, PR #80).
- **Canonical browse SQL emitter** (#77, simplified by #85) —
  `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql` + `build_where`
  in [src/localmail/api/browse.py](src/localmail/api/browse.py).
- **Folder-filter uses EXISTS semi-join** (#85, PR #86).
- **content_id chain is end-to-end covered** (PR #88).
- **vite 6.4.2 + vitest 3.2.4 in gui/** (PR #89). Do NOT roll
  vitest back to 2.x.
- **`formatRelativeDate` uses LOCAL-time `sameDay`** (PR #92).
- **Pure-helper modules under `gui/src/lib/` follow `*_helpers.ts`**
  (PR #93).
- **Per-domain typed errors for GUI blob-download commands**
  (PR #94, on `main` as `ecf7e34`). `AuthError` no longer
  carries `Io(String)`; attachment + raw-message command
  families have sibling enums composing `AuthError` via
  `#[from]`.

## File map (as of `main` head `ecf7e34`)

```
src/localmail/                              # unchanged this session
  api/messages.py / api/browse.py          # unchanged (PR #86 / #88 invariants)
  config.py / parser.py / attachments.py   # unchanged
  sanitize.py / search/ / serve/           # unchanged
  cli.py / daemon.py / worker.py / ...     # unchanged
migrations/                                # 0001 … 0019 (unchanged)
tests/                                     # 805 passing (baseline; unchanged)
gui/src-tauri/src/commands/                # PR #94 baseline (on main)
  attachments.rs                            # AttachmentError enum (8 variants)
  raw_message.rs                            # RawMessageError enum (subset)
  auth.rs                                   # AuthError without Io variant
  auth_change_password.rs                   # comment refresh
gui/src/lib/                               # PR #94 baseline (on main)
  format_error.ts                          # docstring lists new wrappers
  format_error.test.ts                     # AttachmentError describe block
  change_helpers.ts / change_helpers.test.ts # (PR #93 baseline)
  stores/mail.svelte.ts                    # (PR #93 baseline)
gui/src/screens/settings/                  # PR #94 baseline (on main)
  SettingsServer.test.ts                   # transient mock = nested Http>Network
docs/handoffs/
  2026-05-25T0522-utc-housekeeping-post-pr-94.md  # THIS session's snapshot
  2026-05-24T2312-utc-attachment-error-pr-94.md   # prior (PR #94 author session)
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

End of housekeeping session. PR #94 merged externally; local
branch deleted; no code authored. Open issue count: 10.
Next: smoke #91 (Tauri dev/build — needs maintainer at
keyboard with display) is the recommended pick; #24 (macOS CI
matrix) is the only autonomous-friendly remaining item but
spends real CI minutes.
