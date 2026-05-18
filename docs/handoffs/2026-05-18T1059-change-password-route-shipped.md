# NEXT_SESSION.md — localmail GUI client handoff

> **Delete this file once Sub-plan 5's B24 bundle + B25 smoke ship and the
> `gui-client-5` PR merges to `main`.**

This session resolved **Open Decision #2** — the server-side
`POST /v1/auth/change-password` route — so the GUI client's
`change_password_cmd` will hit a real endpoint instead of a 404 during B25's
manual smoke. The previously-listed B24 (Tauri bundle build + branded
icons) and B25 (end-to-end smoke + PR open) still need a manual
user-validation pass and are not safe to dispatch as background subagents.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Hybrid search (Phases 1 + 2
incl. attachment text) shipped. GUI server (`localmail serve`, migration 0014)
shipped. GUI client Sub-plans 1–4 shipped and merged to `main`. Sub-plan 5
(this session) closes the GUI feature surface and starts the bundle work.
See [CLAUDE.md](CLAUDE.md) and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

**Server vs. client branch:** the GUI server code (`src/localmail/api/`,
`src/localmail/serve/`, migrations through `0014_api_users.sql`) lives on the
long-lived `worktree-phase2-hybrid-search` branch. Main has the **client**
only. Sub-plan 5's Phase A commits landed on `worktree-phase2-hybrid-search`
directly (no PR — same convention as Sub-plan 4 Phase A). Phase B lives on
the `gui-client-5` branch which will PR into `main` once B24/B25 ship.

## What's done

| Component | Status |
|---|---|
| **Server**: `localmail.api` + FastAPI `localmail serve` + migration 0014 | ✅ PR #6 → `worktree-phase2-hybrid-search` |
| **Sub-plan 1**: Tauri 2 + Svelte 5 scaffolding | ✅ PR #14 → `main` |
| **Sub-plan 2**: Connection core | ✅ PR #19 → `main` |
| **Sub-plan 3**: 3-pane main view shell | ✅ PR #20 → `main` |
| **Sub-plan 4 — Phase A**: Server `account_ids`/`folder_ids` filters | ✅ on `worktree-phase2-hybrid-search` |
| **Sub-plan 4 — Phase B**: GUI search + HTML body + attachments | ✅ PR #21 → `main` |
| **PR #23 fixissues** | ✅ → `main` |
| **Sub-plan 5 — Phase A** (prior session): `lang:` token + filter forwarding | ✅ pushed to `worktree-phase2-hybrid-search` (`23fee78`, `9c23134`) |
| **Sub-plan 5 — Phase B** (prior session): batches 1–4 + bundle config | ✅ pushed to `gui-client-5` (5 commits, `bbdc4c2` HEAD) |
| **Open Decision #2** (this session): server `POST /v1/auth/change-password` | ✅ pushed to `worktree-phase2-hybrid-search` (`4e3a7b1`) |
| **Sub-plan 5 — B24/B25** | 🟡 **bundle smoke + manual smoke + PR — pending user-validation session** |

## What prior sessions shipped

### Phase A — server (`worktree-phase2-hybrid-search`)

| SHA | What |
|---|---|
| `23fee78` | `feat(search): lang: DSL token + languages predicate in _filter_sql` — adds migration `0015_messages_body_lang.sql` (nullable text column + partial index); `parse_query` accepts `lang:VAL`; `_filter_sql` emits `m.body_lang = ANY(%s)` |
| `9c23134` | `feat(api): forward date_from/date_to/lang filters; clear unsupported list` — `_KNOWN_UNSUPPORTED_FILTER_KEYS` is now empty; `_filter_tokens` emits `after:`/`before:`/`lang:` from the three keys |

Phase A tests: full suite (359 passed) green; new tests cover the DSL token,
the languages predicate, and API-layer round-trips.

### Phase B — client (`gui-client-5`)

| SHA | What |
|---|---|
| `50901fe` | (pre-session) Sub-plan 5 plan |
| `d2e1737` | **Batch 1** — pure helpers (splitter, change_poller, version_check), version store, type extensions on `SearchFiltersUI` + `filter_parse`, 4 Tauri commands (`get_version_cmd`, `get_message_raw_cmd`, `get_message_full_headers_cmd`, plus optional `headers` field on `MessageDetail`) |
| `edfbf83` | **Batch 2** — 7 new components (Splitter, VersionGate, RawBodyView, HeaderUnfold, DebugBadges, DebugChunks) + multi-page PDF nav in `AttachmentPreviewModal` |
| `30ed7ac` | **Batch 3** — settings store + SettingsScreen with 4 tabs (Server/Display/Search/About), `change_password_cmd` Tauri command, mail-store polling (`changeCursor`/`pollOnce`/`startPolling`/`stopPolling`/`mergeNewMessages`) |
| `7f1148e` | **Batch 4** — screen integrations: Splitter ×2 + polling + VersionGate in MainView; VersionGate in Router; RawBodyView + HeaderUnfold + DebugChunks in ReadingPane; `dateFrom`/`dateTo`/`language` form fields in FilterPopover |
| `f2862d8` | `quit_app_cmd` Tauri command + tauri.conf.json bundle category/descriptions/macOS minimumSystemVersion |
| `bbdc4c2` | README DSL operator + `lang:` documentation |

## What this session shipped

### Server `change-password` endpoint (`worktree-phase2-hybrid-search`)

| SHA | What |
|---|---|
| `4e3a7b1` | `feat(api+serve): POST /v1/auth/change-password` — adds service-level `change_password(conn, user_id, old, new)` in `localmail.api.auth` (uses the `_DUMMY_PASSWORD_HASH` for timing-stable behavior on unknown user ids) plus the FastAPI route at `/v1/auth/change-password`. Existing bearer tokens stay valid after rotation — a password change is not a logout. **+12 tests** (7 service, 5 route). Full server suite: **371 passed** (was 359). |

Resolves Open Decision #2 — the GUI's `change_password_cmd` (Tauri command
shipped on `gui-client-5` in batch 3) now has a real endpoint to talk to.

### Test counts (acceptance gates — unchanged from prior session except server)

| Suite | Before | After | Plan target |
|---|---:|---:|---|
| **Server pytest** | 359 passed | **371 passed** | n/a |
| **GUI vitest** | 105 | 215 (unchanged this session) | 150+ ✅ |
| **GUI cargo test** | 49 | 69 (unchanged this session) | 60+ ✅ |
| **`npm run check`** | — | 0 errors (unchanged this session) | 0 errors ✅ |
| `_KNOWN_UNSUPPORTED_FILTER_KEYS` | `{date_from, date_to, lang}` | `frozenset()` | empty ✅ |

## What remains — B24 + B25

The two remaining tasks need a host with a working Tauri bundle toolchain
and human visual validation.

### Task B24 — Tauri bundle + branded icons

**Already done in `f2862d8`:**
- `bundle.category = "Productivity"`, short/long descriptions, macOS
  `minimumSystemVersion: 10.15`.
- Existing default Tauri icons in `gui/src-tauri/icons/` remain
  (32×32 / 128×128 / 128×128@2x / .icns / .ico).

**Still needs the user to do:**
1. Replace the default Tauri icons with a branded asset. The plan calls
   for a simple SVG envelope+database silhouette via `rsvg-convert` /
   ImageMagick → `1024×1024 icon.png` → Tauri-generated platform sizes
   via `npx @tauri-apps/cli icon ./icons/icon.png`. Skip if a designer
   has provided final art.
2. Run a macOS bundle smoke:
   ```bash
   cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui
   npm run tauri build -- --bundles dmg
   open src-tauri/target/release/bundle/dmg/*.dmg   # mount + run
   ```
3. Document the Windows `.msi` / Linux `.AppImage` build commands in
   `docs/superpowers/notes/2026-05-18-bundle-smoke.md` (CI can later
   add per-OS jobs).

### Task B25 — manual smoke + PR

Manual click-through against a real `localmail serve` instance:

1. Connect / Login / settings persistence (localStorage key
   `localmail.gui.settings` + `localmail.gui.paneWidths`).
2. Resize the splitter, restart the app, check widths persist.
3. Toggle settings.debug, verify DebugBadges chips render in
   MessageList rows and DebugChunks section in ReadingPane (matched
   chunks visible if the server returns `matched_chunks`).
4. Open a message, switch body mode HTML → Plain → Raw (RawBodyView).
5. Toggle HeaderUnfold; verify it lazy-fetches via
   `get_message_full_headers_cmd?headers=full`.
6. Type `lang:de before:2025-01-01 conference` in SearchBar — verify
   results.
7. Open Settings → change-password — should now succeed end-to-end against
   `worktree-phase2-hybrid-search` HEAD (commit `4e3a7b1`). Verify: enter
   wrong old password → toast/error, 401 surfaces; enter correct old +
   non-empty new → success; re-login with new password works; existing
   token still works for `/v1/auth/whoami` (intentional — rotation does
   not log you out of your current session).
8. Open the GitHub PR:
   ```bash
   gh pr create --base main --head gui-client-5 \
     --title "GUI client Sub-plan 5: polish + packaging" \
     --body-file docs/superpowers/plans/2026-05-18-localmail-gui-client-5-polish-packaging.md
   ```

## Open decisions & risks

1. **`messages.body_lang` is NULL for every existing row.** Migration
   0015 adds the column but no embed-worker change populates it.
   `lang:` filtering will therefore return 0 results until either (a)
   a follow-up embed worker change detects language per-message, or (b)
   a one-shot backfill script runs. The plumbing is correct; data
   population is deferred.

2. ~~**`POST /v1/auth/change-password` is NOT implemented on the server.**~~
   **Resolved this session** (commit `4e3a7b1` on
   `worktree-phase2-hybrid-search`). The plan's Step 1 sketch referenced
   non-existent helpers (`verify_password(conn, …)`, `update_password(conn, …)`);
   the actual implementation lives as `localmail.api.auth.change_password`
   and re-uses the existing `verify_password(password, hash)` + a fresh
   `hash_password` call inside one transaction. Existing tokens intentionally
   stay valid after rotation (rotation ≠ logout). Will merge to `main` when
   the rest of the GUI server work merges.

3. **`VersionGate` calls `quit_app_cmd`.** Shipped as a tiny Rust
   command in `gui/src-tauri/src/lib.rs` (commit `f2862d8`) that uses
   `app.exit(0)`. The component also has a `window.close()` fallback.
   No follow-up needed.

4. **`MessageDetail.matched_chunks` is optional and may not be in any
   current server response.** `DebugChunks` degrades gracefully when
   absent. A future server-side enhancement could populate it without
   GUI changes.

5. **VersionGate mounted twice** (Router + MainView). Both render
   only on api_major mismatch so the double-mount is harmless, but a
   minor cleanup could remove one — defer until the manual smoke
   confirms behaviour.

6. **The `B5`-via-`B15` race during batch 3 produced a transient
   12-failure window** before sibling agents finished. Reproduced
   from agent reports — fixed once all batch-3 agents completed (final
   state: all 204 tests green). If recreating the workflow, run B5
   *after* B15 to avoid the noise.

7. **Plan deviations** (recorded in agent reports):
   - `imagePolicy` has 3 values (`block | ask | allow`) instead of
     the plan's 2 — both UI and tests handle this.
   - Cargo `VersionInfo` struct uses the 4-field `server_version` /
     `build_hash` shape (matching what the TS agent had already shipped),
     not the 5-field shape the plan sketched.
   - Tests use `vi.mock` + `vi.hoisted` rather than `vi.spyOn` —
     matches the existing `auth.test.ts` pattern.

## Exact commands to resume

```bash
# Get to the worktree:
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5

# Verify still-green baseline:
cd gui
npm test -- --run                  # → 33 files, 215 tests passing
(cd src-tauri && cargo test)       # → 69 tests passing
npm run check                      # → 0 errors

# Run the server locally for the manual smoke (separate terminal):
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
unset VIRTUAL_ENV && uv run pytest -q   # → 371 passed (sanity)
unset VIRTUAL_ENV && uv run localmail serve --bind 127.0.0.1 --port 8443 \
                                            --tls-cert PATH --tls-key PATH

# B24: bundle smoke (after replacing icons if branded):
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui
npm run tauri build -- --bundles dmg
open src-tauri/target/release/bundle/dmg/*.dmg

# B25: open PR after manual smoke
gh pr create --base main --head gui-client-5 \
  --title "GUI client Sub-plan 5: polish + packaging" \
  --body "Closes the v1 GUI feature surface. See plan + handoff."

# Note: change-password endpoint already shipped to the server worktree
# (commit 4e3a7b1) — no server-side TODO remaining for Sub-plan 5.
```

## Known gotchas (still load-bearing — don't repeat them)

All gotchas from prior handoffs still apply. Specific to Sub-plan 5:

- **`@testing-library/jest-dom` is NOT installed.** Use `toBeTruthy()` /
  `.textContent` / `querySelector` checks. Never `toBeInTheDocument()`.
- **jsdom lacks `window.localStorage` by default.** `settings.test.ts`,
  `mail.test.ts`, and `MainView.test.ts` install an in-memory shim
  before module import. Copy the pattern for any new test that needs
  localStorage.
- **jsdom lacks `PointerEvent`.** `Splitter.test.ts` polyfills a
  `MouseEvent` subclass in `beforeAll` so the plan's drag assertions
  pass.
- **Rust command argument structs need `#[derive(Deserialize)]`.**
- **`vi.mock("@tauri-apps/api/core", ...)` is the preferred mocking
  pattern** — see `src/lib/stores/auth.test.ts` for the `vi.hoisted`
  template.
- **`unset VIRTUAL_ENV && uv run …`** — required for ad-hoc Python
  commands so the wrong venv doesn't get picked up.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql://localmail:local%40%40mail@localhost:5532/localmail_test`.**
  If your local PG runs on the default port without the `localmail`
  user, override with `LOCALMAIL_TEST_DSN=postgresql:///localmail_test`.
- **Sub-plan 5 introduces ReadingPane sub-modes** (HTML/Plain/Raw);
  `bodyMode` is sticky across messages but `externalImagesAllowed`
  resets per-message — don't conflate them.

## File map (post-Sub-plan 5)

```
docs/superpowers/specs/2026-05-17-localmail-gui-design.md            # design spec
docs/superpowers/plans/2026-05-18-localmail-gui-client-5-polish-packaging.md  # plan (now executed)
docs/handoffs/2026-05-18T1043-sub-plan-5-shipped.md                  # snapshot of prior NEXT_SESSION
docs/handoffs/2026-05-18T1059-change-password-route-shipped.md       # snapshot of THIS doc

.claude/worktrees/
  phase2-hybrid-search/                # Phase A + change-password route (4e3a7b1)
  gui-client-5/                        # Phase B landed here (5 commits past plan)

src/localmail/api/auth.py              # +change_password(conn, user_id, old, new)
src/localmail/serve/routes/auth.py     # +POST /v1/auth/change-password
tests/test_api_auth_passwords.py       # +7 service tests
tests/test_serve_auth_routes.py        # +5 route tests
gui/src/                               # +10 components, +6 stores, +6 API wrappers, +1 screen
gui/src-tauri/src/commands/            # +4 new commands (version, raw_message, full_headers, auth_change_password)
gui/src-tauri/src/lib.rs               # +quit_app_cmd
gui/src-tauri/tauri.conf.json          # bundle metadata polished
```

End of change-password-route session. Hand off to a user-validation
session for B24/B25 — the server side is now fully ready for the
Settings → Server → change-password smoke step.
