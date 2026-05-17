# localmail GUI

Desktop client for the [localmail](../) archive. Tauri 2 + Svelte 5 + TypeScript.

## Prereqs

- Node.js 20+ and npm
- Rust 1.80+ (`rustup install stable`)
- Platform webview deps:
  - **macOS**: nothing — webview ships with the system
  - **Linux**: `sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file libssl-dev libayatana-appindicator3-dev librsvg2-dev`
  - **Windows**: WebView2 (preinstalled on Windows 11)

## Develop

```bash
cd gui
npm install        # one-time
npm run tauri dev  # opens a window with hot-reload
```

## Test

```bash
npm test                        # vitest (frontend unit tests)
cd src-tauri && cargo test      # Rust unit tests
npm run check                   # svelte-check (type checks .svelte and .ts)
```

## Build a release bundle

```bash
npm run tauri build
```

Produces a platform-specific bundle in `src-tauri/target/release/bundle/`.

**NOTE:** Bundling uses the icon files at `src-tauri/icons/`. The scaffolding ships with **minimal placeholder PNGs + ICNS** generated programmatically (Sub-plan 1) so `cargo build` and `tauri dev` succeed; real branded icons are part of Sub-plan 5 (Packaging).

**Icon-rebuild gotcha:** `tauri::generate_context!()` embeds icon bytes into the binary at compile time. If you change a file in `src-tauri/icons/`, cargo doesn't see that change (icons aren't in the source dependency graph) — touch `src/lib.rs` or run `cargo clean -p localmail-gui` to force a relink, otherwise the old icon bytes stay baked in and macOS may panic at app launch.

## Manual smoke (Sub-plan 1 acceptance)

After `npm run tauri dev`:

1. A native window titled "localmail" opens.
2. Type a name in the input (default "world") and click "Greet from Rust".
3. The page shows `Hello, <name>! (from Rust)` and below it `via tauri-cmd`.
4. Right-click → "Inspect" (or Cmd+Option+I on macOS) — DevTools opens with no console errors.
5. Close the window — `tauri dev` exits cleanly.

If any of those fail, that's a Sub-plan 1 regression worth fixing before moving on.

## Manual smoke (Sub-plan 2 acceptance)

Requires `localmail serve` running on your machine. Easiest setup:

```bash
# In a separate terminal, from the localmail repo root:
cd .claude/worktrees/phase2-hybrid-search   # or wherever your server checkout lives
unset VIRTUAL_ENV && uv run localmail add-api-user alice         # if alice doesn't exist
unset VIRTUAL_ENV && uv run localmail serve --bind 127.0.0.1     # listens on https://127.0.0.1:8443
```

Then from the gui client worktree:

```bash
cd gui
npm run tauri dev
```

Acceptance steps:

1. App opens to **Connect** screen with `https://localhost:8443` pre-filled.
2. Click "Connect". After ~1s the screen shows the cert SHA-256 fingerprint.
   The fingerprint should be 64 lowercase hex chars.
3. Click "Trust this certificate". App moves to **Login** screen.
4. Enter `alice` / `hunter2` (whatever password you set in step 0 above) and submit.
   App moves to **Authenticated Shell**.
5. Header shows `alice` and capability pills: `search`, `attachments`, `attachment_text`
   light up green; `threading`, `send` are struck-through grey.
6. Click "Refresh token". UI stays on the same screen; no error.
7. Click "Log out". App moves back to Login.
8. Quit the app (Cmd+Q on macOS). Re-launch (`npm run tauri dev` again).
   App should bypass Connect (pin saved) and go straight to Login (token cleared at logout).
9. Log in again. Should land on AuthShell as before.
10. Quit. Re-launch. Should now go straight to AuthShell (token still valid).

If any step fails, capture the offending console output (DevTools → Console) AND the
output of `npm run tauri dev` from the terminal, then report.

### Inspecting the keyring

After successful login, on macOS:

```bash
security find-generic-password -s localmail-gui -a server_url -w
security find-generic-password -s localmail-gui -a username -w
security find-generic-password -s localmail-gui -a cert_sha256_pin -w
security find-generic-password -s localmail-gui -a bearer_token -w
```

These should show your stored values. After logout, only `server_url`, `cert_sha256_pin`
should remain — `username` and `bearer_token` cleared.

## Manual smoke (Sub-plan 3 acceptance)

Same server prereqs as Sub-plan 2. Run `localmail serve` and have at least
one account synced with some messages (otherwise the message list will be
empty — not a bug).

```bash
cd gui
npm run tauri dev
```

Acceptance steps:

1. Log in as before (Sub-plan 2 flow).
2. App lands on the new **Main view** — three columns:
   - Left rail: "📥 All Mail" pinned at top, then your configured accounts.
   - Middle column: a list of the most recent ~200 messages across all
     accounts, sorted newest first. Each row shows sender, subject, account,
     and a relative date.
   - Right pane: "Select a message to read it." placeholder.
3. Click an account in the left rail. The account expands to show its
   folders (loaded from `/v1/accounts/{id}/folders`). The middle column
   filters to messages from that account (**client-side filter on the
   already-loaded 200 — server-side narrowing arrives in Sub-plan 4**).
4. Click a folder. Selection narrows further but the same client-side
   account filter is what's actually applied (folder filtering is also
   server-side and deferred).
5. Click "📥 All Mail" to reset to the full loaded set.
6. Click any message row. The right pane loads its plain-text body and
   key headers (From / To / Date / Account · Folders). HTML-only messages
   show "No plain-text body. (HTML rendering arrives in Sub-plan 4.)" —
   that is expected behaviour for this sub-plan.
7. Click another message; the right pane updates without flicker.
8. Click the same message twice — no redundant network request fires.
9. "Refresh token" and "Log out" buttons in the top header still work.
10. After log out, log back in. The main view loads accounts + messages
    again with no stale data.

If any step fails, capture the DevTools console output AND the `npm run
tauri dev` terminal output, then report.

## Manual smoke (Sub-plan 4 acceptance)

Prereqs: server from Phase A (`account_ids`/`folder_ids` filter wiring) must be
running. If you see a `ValidationFailed: filter 'account_ids' is accepted by
the API schema but not yet wired through to the search backend` chip in the
GUI, Phase A has not been merged into the server build you're hitting.

```bash
cd gui
npm run tauri dev
```

Acceptance steps:

1. Log in (Sub-plan 2 flow).
2. **Tree narrowing is now server-side.** Click an account — the middle pane
   updates to show the server-returned, account-narrowed result set (not a
   client-side filter over the 200-message changes load).
3. Click a folder under an account. Same — server-narrowed.
4. Click "📥 All Mail" — clears `accountIds` / `folderIds` and submits an
   empty query; the middle pane shows the most-recent across-all-accounts
   results.
5. **Search bar.** Type `school` and press Enter — results with subject text
   matching "school" appear, snippets highlight matches with yellow `<mark>`
   background. Caption above the list shows "Search took N ms — M result(s)".
6. **DSL.** Type `from:anna has:attachment after:2024-01-01` — only matching
   messages appear. Chips below the search bar show `From: anna`,
   `After: 2024-01-01`, `Has attachment` — click `×` on the "From: anna" chip
   to remove that one.
7. **Filter popover.** Click "🔧 Filters" — popover opens. Set
   `Subject = invoice`, click Apply. Results filter accordingly; a
   `Subject: invoice` chip appears.
8. **HTML body.** Click any message with an HTML body — reading pane renders
   the HTML inside a sandboxed iframe. External images (if any) are blocked:
   a "Load images for this message" button appears above the body. Click it —
   images load.
9. **Body toggle.** Click "Plain" — switches to plain-text rendering. Click
   "HTML" — switches back. "Raw" shows the deferred placeholder for now.
10. **Attachment download.** Open a message with attachments. Each
    attachment shows as a chip with a Download button. Click Download — save
    dialog appears, choose a destination, file is written.
11. **Image preview.** Open a message with an image attachment, click the 👁
    button on it — modal opens, image is rendered inline. Click backdrop or
    press Escape to close.
12. **PDF preview.** Same with a PDF — first page renders in the modal
    canvas. (Full multi-page paginated view is a Sub-plan 5 polish item.)
13. **Switch messages** — confirm the per-message "Load images" allowance
    resets (a new HTML message starts with images blocked again).
14. Log out and back in — the search store resets, tree clears narrowing.

If any step fails, capture the DevTools console output and `npm run tauri
dev` terminal output, then report.

## Talking to the server

The client expects a `localmail serve` HTTPS endpoint. The connection URL, username, password, and TLS cert pin are stored in the OS keyring — landed in Sub-plan 2 (not in this scaffolding).

## Security baseline

Sub-plan 1 ships a scaffolding-only webview that renders no remote or user-supplied content. Two knobs were called out during review of PR #14:

- **CSP** — [`tauri.conf.json`](src-tauri/tauri.conf.json) now ships a strict default: `default-src 'self'`, IPC channel for `connect-src`, `script-src 'self'`, `style-src 'self' 'unsafe-inline'` (Svelte injects scoped styles inline), `img-src 'self' data:`, plus `object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'`. Tauri auto-appends nonces/hashes for the bundled output. **Sub-plan 4 (HTML email rendering) must NOT loosen this policy** — email body HTML must be sanitized server-side and/or sandboxed in a `srcdoc` iframe with its own CSP, not by punching holes in the app-level policy.
- **`shell:allow-open` scope** — left at the default. Per Tauri 2 docs the default scope is restricted to `http`, `https`, `tel`, and `mailto` schemes — `file:` and `javascript:` are excluded out of the box, so this is already safe for email-link handling. If/when we move to `tauri-plugin-opener` the explicit glob-based scope syntax can pin this further.

Lower-priority follow-ups still tracked:

- Tighten `tsconfig.json` (`noUnusedLocals`, `noUnusedParameters` → `true`) once Sub-plan 2 adds real surface — [issue #17](https://github.com/hherb/localmail/issues/17).
- Add a CI workflow for `gui/` — [issue #18](https://github.com/hherb/localmail/issues/18).
