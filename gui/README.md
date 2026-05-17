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

**NOTE:** Bundling uses the icon files at `src-tauri/icons/`. The scaffolding ships with **minimal placeholder PNGs** generated programmatically (Sub-plan 1) so `cargo build` succeeds; real branded icons are part of Sub-plan 5 (Packaging). Until then, `tauri build` works but produces a bundle with placeholder iconography.

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
