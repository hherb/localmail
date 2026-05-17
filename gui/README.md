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

## Talking to the server

The client expects a `localmail serve` HTTPS endpoint. The connection URL, username, password, and TLS cert pin are stored in the OS keyring — landed in Sub-plan 2 (not in this scaffolding).

## Security baseline

Sub-plan 1 ships a scaffolding-only webview that renders no remote or user-supplied content. Two knobs were called out during review of PR #14:

- **CSP** — [`tauri.conf.json`](src-tauri/tauri.conf.json) now ships a strict default: `default-src 'self'`, IPC channel for `connect-src`, `script-src 'self'`, `style-src 'self' 'unsafe-inline'` (Svelte injects scoped styles inline), `img-src 'self' data:`, plus `object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'`. Tauri auto-appends nonces/hashes for the bundled output. **Sub-plan 4 (HTML email rendering) must NOT loosen this policy** — email body HTML must be sanitized server-side and/or sandboxed in a `srcdoc` iframe with its own CSP, not by punching holes in the app-level policy.
- **`shell:allow-open` scope** — left at the default. Per Tauri 2 docs the default scope is restricted to `http`, `https`, `tel`, and `mailto` schemes — `file:` and `javascript:` are excluded out of the box, so this is already safe for email-link handling. If/when we move to `tauri-plugin-opener` the explicit glob-based scope syntax can pin this further.

Lower-priority follow-ups still tracked:

- Tighten `tsconfig.json` (`noUnusedLocals`, `noUnusedParameters` → `true`) once Sub-plan 2 adds real surface — [issue #17](https://github.com/hherb/localmail/issues/17).
- Add a CI workflow for `gui/` — [issue #18](https://github.com/hherb/localmail/issues/18).
