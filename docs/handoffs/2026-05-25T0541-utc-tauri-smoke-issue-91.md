# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-25T0541 UTC (post-session).**
> **Issue #91 closed** by manual Tauri dev/build smoke against the
> PR #94 IPC envelope changes. No code authored; this was a pure
> verification session.
>
> `main` HEAD remains `ecf7e34` (PR #94). Working tree clean (only
> untracked `.claude/settings.local.json` — local IDE-permission
> file, not part of repo convention to commit).
>
> **Smoke results (acceptance criteria from #91 + the prior handoff)**:
> | Check | Result |
> |---|---|
> | `npm run tauri dev` launch, no panic | ✅ Vite ready in 349ms; cargo cache hit (0.32s); window clean |
> | Login via Settings (`AuthError` envelope) | ✅ |
> | Attachment download (`AttachmentError` envelope, PR #94) | ✅ Native save dialog, original filename, no inline render |
> | Raw-message view (`RawMessageError` envelope, PR #94) | ✅ Bytes rendered, no error banner |
> | Negative path (server killed → `formatError()` banner) | ✅ Clean `"Http: Network: error sending request for url (…)"` — no `[object Object]`, no stale `Io:` variant |
> | `npm run tauri build` produces bundle | ✅ Vite 846ms + cargo release 57.48s → `.app` and `.dmg` (aarch64) |
> | Release `.app` launches via `open` | ✅ Window clean |
>
> **Issue closed**: **#91** (smoke after vite 6.4 / esbuild 0.25 bump).
> **Open issue count: 10 → 9**.
>
> **Prior session (PR #94 author session)**: PR #94 merged
> externally as `ecf7e34`; per-domain typed errors for the GUI
> blob-download command families; closed #22.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

**No code authored.** PR #94 was authored + merged in prior
sessions; this session manually validated the IPC envelope
change end-to-end and closed #91.

### Session artefacts

- **Closed issue #91** with a smoke summary covering all
  acceptance criteria (login, attachment download, raw-message
  view, negative-path `formatError()` banner, release build,
  bundle launch).
- `NEXT_SESSION.md` refreshed to post-#91 state (this file).
- `docs/handoffs/2026-05-25T0541-utc-tauri-smoke-issue-91.md` —
  archived snapshot.

### Session notes worth keeping

- **Negative-path coverage via the polling envelope.** Killing
  `localmail serve` triggers the `/v1/changes` poller's
  `HttpError::Network` envelope, which renders as `"Http:
  Network: error sending request for url (…)"`. The wire shape
  is identical to what `AttachmentError`/`RawMessageError`
  produce on a network failure (same `#[serde(tag="kind",
  content="detail")]` envelope, same variant-agnostic JS
  walker), so the polling-path banner validates the
  attachment + raw-message-path banners end-to-end for the same
  renderer. No need to fabricate a per-path negative test if the
  renderer is exercised by any other path.
- **Tauri dev exits cleanly on window close** (exit 0 from
  `npm run tauri dev`). Two dev sessions in a row exited that
  way after window close; not a regression.
- **Vite re-optimization (`pdfjs-dist`) on first launch may
  trigger a reload** — saw it once, subsequent launches skipped
  it. If a smoke run shows a reload banner, just re-launch.

### Docs review

- **README.md** — checked, no AuthError mention; unchanged.
  PR #94's IPC envelope rename is internal to `gui/src-tauri/`
  and never surfaces in user-facing docs.
- **ROADMAP.md** — does not exist in this repo.
- **CLAUDE.md** — unchanged. The PR #94 per-domain-enum pattern
  is captured in the prior handoff
  (`2026-05-24T2312-utc-attachment-error-pr-94.md`) and in the
  in-source docstrings; not promoted to CLAUDE.md.

## What's next

### 1. **#28 — encoding toggle / charset detection for `RawBodyView`** *(GUI polish; bundle with Tauri dev session)*

Now that #91 is closed and the smoke confirms `RawBodyView`
renders bytes cleanly via the new `RawMessageError` envelope,
#28 is the natural follow-up. The issue body proposes:
1. Sniff a likely encoding from `Content-Type: charset=` (or
   use the server-decoded `body_text` as a hint).
2. Surface a small dropdown next to the existing controls
   (UTF-8 / Latin-1 / Windows-1252 / auto).

`TextDecoder("windows-1252")` etc. are WebView-supported, no
new deps. Implementation is ~one component + a charset-sniff
pure function with unit tests. **Visual verification needs
Tauri dev** — bundle with the next interactive session (i.e.
*don't* attempt this purely headless; the dropdown needs to be
eyeballed).

**Acceptance**:
- Charset-sniff pure function with vitest coverage
  (Content-Type parsing, fallback heuristic).
- Dropdown integrated into `RawBodyView.svelte` with
  UTF-8 / Latin-1 / Windows-1252 / shift_jis / auto.
- Manual smoke: open a message known to be Latin-1 (or fabricate
  one); confirm "Auto" sniffs correctly; confirm manual override
  flips the rendering live (no page reload).

### 2. **#38 — `/v1/changes` semantics decision** *(needs user input)*

Three options outlined in the issue body:
1. Tail-subscription + `/v1/messages` backfill.
2. `min_id` for backward sweep.
3. Strict tail.

Needs telemetry from the GUI client + a design call. The issue
cannot progress without a maintainer-side decision; this is not
a pure-code task.

### 3. **#24 — macOS to gui-ci.yml OS matrix** *(headless-doable; spends CI minutes)*

Concrete YAML change: convert `tauri-rust` job to a
`strategy.matrix.os: [ubuntu-latest, macos-latest]`. The cost
concern (macOS minutes are 10× pricier) means run-on-draft-PR
first.

**Acceptance**: gui-ci.yml runs both `ubuntu-latest` +
`macos-latest`; both report SUCCESS on a clean PR.

The most autonomous-friendly remaining item — but the CI run
itself is the test (can't validate the change without spending
the minutes).

### 4. **#87 — CI-gated at-scale regression coverage for folder-filter** *(needs scale-tuning + decision)*

Two options in the issue body:
1. Plan-signature assertion at smaller scale (assert no `Unique`
   node, no full-projection Sort on the broad-folder probe).
2. Buffer-hit ceiling test.

Substantial: scale-tuning is empirical (the DISTINCT-regression
signature only surfaces at PG-version-dependent scales).

### Blocked / deferred

- **#90** glib Cargo alert — upstream-blocked. Action only when
  Tauri ships a release with gtk-rs ≥ 0.19 / glib ≥ 0.20.
- **#25** websockets.legacy DeprecationWarning — blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (follow-up to #36).
  Gated on real ops data.
- **#5** Search batch INSERT for chunking loop — issue says
  "Defer until someone actually measures backfill time on a
  large archive."
- **#2** Migration 0006 GIN CONCURRENT for live-upgrade — issue
  says "deferring until someone actually hits the live-upgrade
  scenario."

## Open decisions & risks

1. **Empty "what we shipped" is intentional (again).** This
   session validated PR #94 via manual smoke and closed #91 —
   no new code. The handoff exists because `/nextsession`
   always refreshes `NEXT_SESSION.md` + archives a snapshot.

2. **Negative-path coverage relies on the polling envelope.**
   The smoke did not click "download attachment" specifically
   with serve down — instead, the periodic `/v1/changes` poller
   triggered the same `formatError()` walker on a `HttpError`
   envelope identical in shape to the new `AttachmentError`
   /`RawMessageError` envelopes. The risk is low (the renderer
   is variant-agnostic, the Rust-side wire-shape tests pin the
   attachment-path serialisation), but if a future change makes
   the renderer variant-specific, the polling envelope no
   longer covers the attachment path. Add an explicit
   per-path negative test in that case.

3. **Two `localmail.app` processes alive after `open`** during
   the release-build verification — a stale `/Applications/
   localmail.app` from an earlier install was launched
   alongside the fresh `gui/src-tauri/target/release/bundle/
   macos/localmail.app`. Both windows came up clean. Cleanup is
   the maintainer's call; if you `cp` the new bundle into
   `/Applications/`, kill the stale process first or you'll see
   duplicate windows on subsequent `open` calls.

4. **`reqwest::Error` Display formatting is a load-bearing
   string.** The negative-path banner reads `"error sending
   request for url (https://localhost:8443/v1/changes)"` — that's
   reqwest's `Error::Display` impl. If reqwest changes the
   wording across a major bump, the operator-facing banner
   wording will shift, but the wire shape (`{kind, detail}`)
   stays intact. Don't pin tests against the wording; the unit
   tests in `format_error.test.ts` correctly assert structure
   not strings.

5. **Carried-forward invariants (still load-bearing)** — same
   list as the prior handoffs; not duplicated here. Full
   enumeration in
   [`docs/handoffs/2026-05-24T2312-utc-attachment-error-pr-94.md`](docs/handoffs/2026-05-24T2312-utc-attachment-error-pr-94.md)
   "Carried forward from prior sessions". Nothing in that list
   changed this session.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin
git checkout main && git pull        # HEAD = ecf7e34
git status                            # clean

# Backend sanity (untouched this session):
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                   # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail    # 4 pre-existing parser.py errors

# GUI sanity (PR #94 baseline, on main):
cd gui/src-tauri && cargo test --locked    # expect 79 passed
cd gui/src-tauri && cargo clippy --locked -- -D warnings   # expect clean
cd gui && npm run check                    # expect 0/0/333
cd gui && npm test                         # expect 276 passed

# Pick next piece:
gh issue list --state open --limit 40

# Recommended: #28 (charset toggle for RawBodyView; needs Tauri dev for visual smoke):
#   Start a local serve + tauri dev:
#     unset VIRTUAL_ENV && uv run localmail serve \
#       --bind 127.0.0.1 --port 8443 \
#       --tls-cert ~/.config/localmail/tls/cert.pem \
#       --tls-key ~/.config/localmail/tls/key.pem
#     cd gui && npm run tauri dev
#   Implementation:
#     1. New pure-function module gui/src/lib/charset_helpers.ts with vitest cover
#        (parse Content-Type charset=, sniff fallback heuristic).
#     2. Update gui/src/components/RawBodyView.svelte to wire a dropdown +
#        the charset_helpers detector.
#     3. Manual smoke: open a Latin-1 message; confirm auto sniff + manual override.

# If picking #24 (macOS CI matrix):
#   git checkout -b ci/gui-add-macos-matrix
#   Edit .github/workflows/gui-ci.yml:
#     - tauri-rust job: add strategy.matrix.os: [ubuntu-latest, macos-latest]
#                       + fail-fast: false
#                       + runs-on: ${{ matrix.os }}
#   Push as DRAFT first to confirm macOS minute consumption before un-drafting.

# Smoke replay (if you want to re-verify #91):
#   Same commands as above (serve + tauri dev). Smoke checklist:
#     1. Login via Settings.
#     2. Download attachment (confirm save dialog, not inline render).
#     3. View raw message (confirm bytes render).
#     4. Negative path: kill serve PID; confirm error banner reads
#        "Http: Network: …" with reqwest's url in parens; no Io variant.
#     5. cd gui && npm run tauri build; open
#        gui/src-tauri/target/release/bundle/macos/localmail.app
```

## Known gotchas (still load-bearing)

Same enumeration as the prior handoff. Quick reference (full
detail in `docs/handoffs/2026-05-24T2312-utc-attachment-error-pr-94.md`):

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a
  stale `VIRTUAL_ENV`.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`**.
- **Next migration would be `0020_*.sql`** (latest is `0019`).
- **Wire `date` = `COALESCE(internal_date, date_sent)`** on
  every paginated list endpoint.
- **Two cursor flavours on `/v1/search`** — `"<token>:<page>"`
  (pool) and `"K|<base64>"` (keyset).
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
- **NEW from this session: smoke-validated PR #94 end-to-end.**
  Login + attachment download + raw view + negative-path
  banner + release bundle all clean. Renderer is variant-
  agnostic; polling-envelope negative test transitively
  covers attachment + raw-message paths.

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
  format_error.ts / format_error.test.ts   # AttachmentError describe block
  change_helpers.ts / change_helpers.test.ts # (PR #93 baseline)
  stores/mail.svelte.ts                    # (PR #93 baseline)
gui/src/screens/settings/                  # PR #94 baseline (on main)
  SettingsServer.test.ts                   # transient mock = nested Http>Network
docs/handoffs/
  2026-05-25T0541-utc-tauri-smoke-issue-91.md          # THIS session's snapshot
  2026-05-25T0522-utc-housekeeping-post-pr-94.md       # prior (PR #94 merge housekeeping)
  2026-05-24T2312-utc-attachment-error-pr-94.md        # prior (PR #94 author)
  2026-05-24T2251-utc-rename-change-helpers-pr-93.md   # prior
  2026-05-24T2236-utc-format-test-fix-pr-92.md         # prior
  2026-05-23T1032-utc-vite-vitest-bump-pr-89.md        # prior
  2026-05-23T0956-utc-housekeeping-post-pr-88.md       # prior
  2026-05-23T0907-utc-content-id-pr-88.md              # prior
  2026-05-23T0755-utc-exists-semi-join-pr-86.md        # prior
  2026-05-23T0308-utc-pr73-followup-pr-84.md           # prior
  2026-05-22T0942-utc-harness-cleanup-pr-83.md         # prior
  2026-05-22T0721-utc-folder-filter-pr-82.md           # prior
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md    # prior
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md     # prior
NEXT_SESSION.md                            # this file (post-session)
```

End of #91 smoke session. Tauri dev + release build both
clean against PR #94's IPC envelope changes. Open issue count:
**9**. Next: **#28** (charset toggle for `RawBodyView`,
needs Tauri dev) is the natural follow-up; **#24** (macOS CI
matrix) is the only autonomous-friendly remaining item.
