# localmail GUI Client — Sub-plan 1: Scaffolding

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a minimal Tauri 2 + Svelte 5 + TypeScript desktop application at `gui/` in the repo. Done when `npm run tauri dev` opens a window that displays "localmail" and clicks a button to roundtrip data through a Rust `greet` command via Tauri's `invoke()`.

**Architecture:** Top-level `gui/` directory holds an independent Tauri project: Rust crate in `gui/src-tauri/`, Svelte+TS frontend in `gui/src/`. Build with Vite (frontend) + `cargo tauri build` (full bundle). No Python in the client.

**Tech Stack:** Tauri 2.x, Svelte 5.x + TypeScript 5.x, Vite 5.x, Rust (whatever stable rustc the user has), Node.js 20+, npm.

**Base branch:** `main`. The client has no on-disk dependency on the server (talks over HTTPS to a separately-running `localmail serve`). Branch from main.

**Out of scope for this sub-plan:**
- Any HTTP client code (Sub-plan 2)
- Any auth / login / TOFU UI (Sub-plan 2)
- Any layout-A 3-pane work (Sub-plan 3)
- Search, reading pane, settings (Sub-plans 4–5)
- Production icons / bundling (Sub-plan 5; this plan accepts that `cargo tauri build` will fail until icons exist)

---

## Task 0: Worktree + tooling prerequisites

**Files:**
- Create worktree at: `.claude/worktrees/gui-client-1/`

- [ ] **Step 1: Create the worktree off main**

```bash
cd /Users/hherb/src/localmail
git fetch --all
git worktree add .claude/worktrees/gui-client-1 -b gui-client-1 main
cd .claude/worktrees/gui-client-1
git log --oneline -1
```

Expected: HEAD is the current tip of `main`. All subsequent tasks run from `/Users/hherb/src/localmail/.claude/worktrees/gui-client-1`.

- [ ] **Step 2: Verify required tools are present**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1
node --version              # expect v20.x or v22.x
npm --version               # expect 10.x+
rustc --version             # expect 1.80+
cargo --version             # expect 1.80+
xcode-select -p             # macOS only — expect a path, not an error
```

If `node` or `npm` is missing: install Node.js 22 LTS from nodejs.org or `brew install node`. If `rustc`/`cargo` is missing: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`. If `xcode-select -p` errors: `xcode-select --install`.

If any tool is missing AND auto-install isn't possible, report BLOCKED with which tool and stop.

- [ ] **Step 3: Add to .gitignore**

Edit `/Users/hherb/src/localmail/.claude/worktrees/gui-client-1/.gitignore`. Append:

```
# GUI client
gui/node_modules/
gui/dist/
gui/src-tauri/target/
gui/src-tauri/gen/
gui/.svelte-kit/
```

- [ ] **Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore(gui-client): gitignore for gui/ build artefacts"
```

---

## Task 1: `gui/` directory + frontend toolchain config

**Files:**
- Create: `gui/package.json`
- Create: `gui/vite.config.ts`
- Create: `gui/svelte.config.js`
- Create: `gui/tsconfig.json`
- Create: `gui/tsconfig.node.json`
- Create: `gui/index.html`

- [ ] **Step 1: Make the gui directory and basic configs**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1
mkdir -p gui
```

- [ ] **Step 2: Write `gui/package.json`**

```json
{
  "name": "localmail-gui",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "check": "svelte-check --tsconfig ./tsconfig.json",
    "tauri": "tauri",
    "test": "vitest run"
  },
  "dependencies": {
    "@tauri-apps/api": "^2.1.1"
  },
  "devDependencies": {
    "@sveltejs/vite-plugin-svelte": "^5.0.1",
    "@tauri-apps/cli": "^2.1.0",
    "@tsconfig/svelte": "^5.0.4",
    "svelte": "^5.1.16",
    "svelte-check": "^4.1.1",
    "tslib": "^2.8.1",
    "typescript": "^5.6.3",
    "vite": "^5.4.11",
    "vitest": "^2.1.8",
    "@testing-library/svelte": "^5.2.6",
    "jsdom": "^25.0.1"
  }
}
```

- [ ] **Step 3: Write `gui/vite.config.ts`**

```ts
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Tauri expects a fixed port; fail if it can't bind.
const host = process.env.TAURI_DEV_HOST;

export default defineConfig(async () => ({
  plugins: [svelte()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
}));
```

- [ ] **Step 4: Write `gui/svelte.config.js`**

```js
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

export default {
  preprocess: vitePreprocess(),
};
```

- [ ] **Step 5: Write `gui/tsconfig.json`**

```json
{
  "extends": "@tsconfig/svelte/tsconfig.json",
  "compilerOptions": {
    "target": "ESNext",
    "useDefineForClassFields": true,
    "module": "ESNext",
    "resolveJsonModule": true,
    "allowJs": true,
    "checkJs": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "moduleResolution": "bundler",
    "strict": true,
    "noImplicitAny": true,
    "noUnusedLocals": false,
    "noUnusedParameters": false,
    "skipLibCheck": true,
    "types": ["svelte", "vitest/globals", "@testing-library/jest-dom"]
  },
  "include": ["src/**/*.ts", "src/**/*.svelte"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

- [ ] **Step 6: Write `gui/tsconfig.node.json`**

```json
{
  "compilerOptions": {
    "composite": true,
    "skipLibCheck": true,
    "module": "ESNext",
    "moduleResolution": "bundler",
    "allowSyntheticDefaultImports": true,
    "strict": true
  },
  "include": ["vite.config.ts"]
}
```

- [ ] **Step 7: Write `gui/index.html`**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>localmail</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
```

- [ ] **Step 8: Install deps**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1/gui && npm install
```

Expected: `node_modules/` is created; no errors. There may be deprecation warnings from transitive deps — those are fine.

- [ ] **Step 9: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1
git add gui/package.json gui/package-lock.json gui/vite.config.ts gui/svelte.config.js gui/tsconfig.json gui/tsconfig.node.json gui/index.html
git commit -m "feat(gui-client): scaffold gui/ — package.json + vite/svelte/ts config"
```

---

## Task 2: Svelte frontend skeleton

**Files:**
- Create: `gui/src/main.ts`
- Create: `gui/src/App.svelte`
- Create: `gui/src/app.css`
- Create: `gui/src/vite-env.d.ts`

- [ ] **Step 1: Write `gui/src/main.ts`**

```ts
import "./app.css";
import { mount } from "svelte";
import App from "./App.svelte";

const app = mount(App, {
  target: document.getElementById("app") as HTMLElement,
});

export default app;
```

- [ ] **Step 2: Write `gui/src/App.svelte`**

```svelte
<script lang="ts">
  let greeting: string = $state("");
  let name: string = $state("world");

  function setGreeting(): void {
    greeting = `Hello, ${name}!`;
  }
</script>

<main class="container">
  <h1>localmail</h1>
  <p class="tagline">read-only archive browser — scaffolding only</p>

  <form
    onsubmit={(e: Event) => {
      e.preventDefault();
      setGreeting();
    }}
  >
    <input id="greet-input" bind:value={name} placeholder="who?" />
    <button type="submit">Greet</button>
  </form>

  {#if greeting}
    <p class="greeting">{greeting}</p>
  {/if}
</main>
```

- [ ] **Step 3: Write `gui/src/app.css`**

```css
:root {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  color: #222;
  background: #fafafa;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  padding: 0;
}

.container {
  max-width: 640px;
  margin: 64px auto;
  padding: 24px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

h1 {
  margin: 0 0 4px;
  font-size: 24px;
}

.tagline {
  margin: 0 0 24px;
  color: #888;
  font-size: 13px;
}

form {
  display: flex;
  gap: 8px;
}

input {
  flex: 1;
  padding: 8px 12px;
  border: 1px solid #ccc;
  border-radius: 4px;
  font-size: 14px;
}

button {
  padding: 8px 16px;
  border: 1px solid #6aa5ff;
  background: #eef5ff;
  color: #1a4fc7;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
}

button:hover {
  background: #d9e8ff;
}

.greeting {
  margin-top: 16px;
  padding: 12px;
  background: #f0f8e8;
  border-left: 3px solid #6aaa3f;
  font-family: ui-monospace, SFMono-Regular, monospace;
}
```

- [ ] **Step 4: Write `gui/src/vite-env.d.ts`**

```ts
/// <reference types="svelte" />
/// <reference types="vite/client" />
```

- [ ] **Step 5: Verify the dev server starts (browser-only sanity, no Tauri yet)**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1/gui && timeout 8 npm run dev 2>&1 | tail -20 || true
```

Expected output includes `VITE v5.x.x ready` and a `Local: http://localhost:1420/` line. If the command hangs without that output, vite has a config error — inspect the output. If you see only the `ready` line and no errors, success. The `timeout` kills the dev server after 8s so the task can complete.

- [ ] **Step 6: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1
git add gui/src/main.ts gui/src/App.svelte gui/src/app.css gui/src/vite-env.d.ts
git commit -m "feat(gui-client): minimal Svelte 5 frontend skeleton"
```

---

## Task 3: Rust crate skeleton (Tauri 2)

**Files:**
- Create: `gui/src-tauri/Cargo.toml`
- Create: `gui/src-tauri/build.rs`
- Create: `gui/src-tauri/tauri.conf.json`
- Create: `gui/src-tauri/src/main.rs`
- Create: `gui/src-tauri/src/lib.rs`
- Create: `gui/src-tauri/capabilities/default.json`
- Create: `gui/src-tauri/icons/.gitkeep`

- [ ] **Step 1: Create directory structure**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1
mkdir -p gui/src-tauri/src gui/src-tauri/capabilities gui/src-tauri/icons
```

- [ ] **Step 2: Write `gui/src-tauri/Cargo.toml`**

```toml
[package]
name = "localmail-gui"
version = "0.1.0"
description = "Read-only desktop client for the localmail archive"
authors = ["Horst Herb"]
edition = "2021"
rust-version = "1.80"

# The lib + binary split is the Tauri 2 convention; `lib.rs` defines the
# command set, `main.rs` is a thin entry point.
[lib]
name = "localmail_gui_lib"
crate-type = ["staticlib", "cdylib", "rlib"]

[build-dependencies]
tauri-build = { version = "2.0", features = [] }

[dependencies]
tauri = { version = "2.1", features = [] }
tauri-plugin-shell = "2.0"
serde = { version = "1", features = ["derive"] }
serde_json = "1"

[profile.release]
panic = "abort"
codegen-units = 1
lto = true
opt-level = "s"
strip = true
```

- [ ] **Step 3: Write `gui/src-tauri/build.rs`**

```rust
fn main() {
    tauri_build::build()
}
```

- [ ] **Step 4: Write `gui/src-tauri/tauri.conf.json`**

```json
{
  "$schema": "https://schema.tauri.app/config/2",
  "productName": "localmail",
  "version": "0.1.0",
  "identifier": "com.horstherb.localmail",
  "build": {
    "beforeDevCommand": "npm run dev",
    "devUrl": "http://localhost:1420",
    "beforeBuildCommand": "npm run build",
    "frontendDist": "../dist"
  },
  "app": {
    "windows": [
      {
        "title": "localmail",
        "width": 1280,
        "height": 800,
        "minWidth": 800,
        "minHeight": 500,
        "resizable": true,
        "fullscreen": false
      }
    ],
    "security": {
      "csp": null
    }
  },
  "bundle": {
    "active": true,
    "targets": "all",
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.icns",
      "icons/icon.ico"
    ]
  }
}
```

- [ ] **Step 5: Write `gui/src-tauri/src/main.rs`**

```rust
// Prevents an extra console window from appearing on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    localmail_gui_lib::run()
}
```

- [ ] **Step 6: Write `gui/src-tauri/src/lib.rs`**

```rust
//! Tauri command surface for the localmail GUI client.
//!
//! Sub-plan 1 ships only the `greet` demo command. Subsequent sub-plans add
//! HTTP, keyring, TOFU, and the API surface the Svelte UI calls into.

use serde::Serialize;

#[derive(Serialize)]
pub struct Greeting {
    pub message: String,
    pub source: &'static str,
}

#[tauri::command]
fn greet(name: &str) -> Greeting {
    Greeting {
        message: format!("Hello, {}! (from Rust)", name),
        source: "tauri-cmd",
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![greet])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::greet;

    #[test]
    fn greet_includes_name_and_marker() {
        let out = greet("world");
        assert!(out.message.contains("world"));
        assert!(out.message.contains("(from Rust)"));
        assert_eq!(out.source, "tauri-cmd");
    }
}
```

- [ ] **Step 7: Write `gui/src-tauri/capabilities/default.json`**

```json
{
  "$schema": "../gen/schemas/desktop-schema.json",
  "identifier": "default",
  "description": "Default capability set for the main window.",
  "windows": ["main"],
  "permissions": [
    "core:default",
    "shell:allow-open"
  ]
}
```

- [ ] **Step 8: Placeholder icons directory**

```bash
touch /Users/hherb/src/localmail/.claude/worktrees/gui-client-1/gui/src-tauri/icons/.gitkeep
```

`cargo tauri dev` does NOT require icon files; `cargo tauri build` does. Icon generation is deferred to Sub-plan 5 (Packaging). Tauri will still launch a dev window without them.

- [ ] **Step 9: Run cargo check on the crate**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1/gui/src-tauri && cargo check 2>&1 | tail -30
```

Expected: `Finished ... profile [unoptimized + debuginfo] target(s)` with no errors. First run downloads all deps and takes 2-5 minutes. Network failures here are usually intermittent — re-run once.

- [ ] **Step 10: Run cargo test**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1/gui/src-tauri && cargo test 2>&1 | tail -10
```

Expected: `test tests::greet_includes_name_and_marker ... ok` and `1 passed`.

- [ ] **Step 11: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1
git add gui/src-tauri/Cargo.toml gui/src-tauri/Cargo.lock gui/src-tauri/build.rs gui/src-tauri/tauri.conf.json gui/src-tauri/src/main.rs gui/src-tauri/src/lib.rs gui/src-tauri/capabilities/default.json gui/src-tauri/icons/.gitkeep
git commit -m "feat(gui-client): Tauri 2 Rust crate skeleton + greet command"
```

---

## Task 4: Tauri ↔ Svelte bridge — wire the `greet` command into the UI

**Files:**
- Modify: `gui/src/App.svelte`
- Create: `gui/src/lib/tauri.ts`

- [ ] **Step 1: Write `gui/src/lib/tauri.ts`**

```ts
/**
 * Thin typed wrappers around Tauri's invoke().
 *
 * Each exported function corresponds to one #[tauri::command] in src-tauri/src/lib.rs.
 * Adding a command means: declare it in Rust, then add a wrapper here.
 */
import { invoke } from "@tauri-apps/api/core";

export interface Greeting {
  message: string;
  source: string;
}

export async function greet(name: string): Promise<Greeting> {
  return invoke<Greeting>("greet", { name });
}
```

- [ ] **Step 2: Update `gui/src/App.svelte` to call the Rust command**

Replace the entire contents of `gui/src/App.svelte` with:

```svelte
<script lang="ts">
  import { greet, type Greeting } from "./lib/tauri";

  let greeting: Greeting | null = $state(null);
  let name: string = $state("world");
  let error: string | null = $state(null);

  async function onGreet(): Promise<void> {
    error = null;
    greeting = null;
    try {
      greeting = await greet(name);
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : String(e);
    }
  }
</script>

<main class="container">
  <h1>localmail</h1>
  <p class="tagline">read-only archive browser — scaffolding only</p>

  <form
    onsubmit={(e: Event) => {
      e.preventDefault();
      void onGreet();
    }}
  >
    <input id="greet-input" bind:value={name} placeholder="who?" />
    <button type="submit">Greet from Rust</button>
  </form>

  {#if greeting}
    <p class="greeting">{greeting.message}</p>
    <p class="meta">via {greeting.source}</p>
  {/if}

  {#if error}
    <p class="error">error: {error}</p>
  {/if}
</main>

<style>
  .meta {
    margin: 4px 0 0;
    font-size: 11px;
    color: #888;
  }
  .error {
    margin-top: 16px;
    padding: 12px;
    background: #fdecea;
    border-left: 3px solid #c0392b;
    color: #c0392b;
    font-family: ui-monospace, SFMono-Regular, monospace;
  }
</style>
```

- [ ] **Step 3: Add a vitest smoke test for the lib/tauri wrapper**

Create `/Users/hherb/src/localmail/.claude/worktrees/gui-client-1/gui/src/lib/tauri.test.ts`:

```ts
import { describe, expect, it, vi } from "vitest";

// Mock the Tauri invoke surface BEFORE importing the wrapper.
vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string, args: Record<string, unknown>) => {
    if (cmd === "greet") {
      return {
        message: `Hello, ${(args as { name: string }).name}! (from Rust)`,
        source: "tauri-cmd",
      };
    }
    throw new Error(`unknown cmd: ${cmd}`);
  }),
}));

import { greet } from "./tauri";

describe("greet wrapper", () => {
  it("forwards name and unwraps the Greeting struct", async () => {
    const out = await greet("alice");
    expect(out.message).toContain("alice");
    expect(out.source).toBe("tauri-cmd");
  });
});
```

- [ ] **Step 4: Run vitest, confirm pass**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1/gui && npm test 2>&1 | tail -20
```

Expected: `1 passed` from the `tauri wrapper` describe block. If vitest can't find `@testing-library/svelte` or `jsdom`, ensure `npm install` from Task 1 completed cleanly.

- [ ] **Step 5: Type-check the whole frontend**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1/gui && npm run check 2>&1 | tail -10
```

Expected: `svelte-check found 0 errors and 0 warnings`. If `$state`/`$derived` rune diagnostics appear, ensure `svelte` is at v5.x in `package.json`.

- [ ] **Step 6: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1
git add gui/src/App.svelte gui/src/lib/tauri.ts gui/src/lib/tauri.test.ts
git commit -m "feat(gui-client): wire greet command into Svelte UI + vitest smoke"
```

---

## Task 5: Manual `tauri dev` verification + README

**Files:**
- Create: `gui/README.md`

- [ ] **Step 1: Launch the dev app manually**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1/gui && npm run tauri dev
```

This is a long-running interactive command. Expected behaviour:
1. Vite starts the dev server on http://localhost:1420
2. Cargo compiles the Rust binary (first run: 2-5 minutes; subsequent: seconds)
3. A native window opens titled "localmail" with the greet UI

**Manual verification steps:**
1. The window opens.
2. Type a name and click "Greet from Rust". Output reads `Hello, <name>! (from Rust)` and below it `via tauri-cmd`.
3. Open DevTools (right-click → "Inspect" or Cmd+Option+I on macOS). Confirm no console errors.
4. Close the window — `tauri dev` exits cleanly.

If the window doesn't open, capture the last 50 lines of stdout/stderr and report BLOCKED. Common failures:
- Missing system webview library (Linux: `sudo apt install libwebkit2gtk-4.1-dev`; macOS: ships with system)
- `cargo` build error from a Tauri version mismatch
- Vite port 1420 already in use (kill the holding process)

- [ ] **Step 2: Write `gui/README.md`**

```markdown
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
npm test           # vitest (frontend)
cd src-tauri && cargo test   # Rust unit tests
npm run check      # svelte-check (type checks .svelte and .ts)
```

## Build a release bundle

```bash
npm run tauri build
```

Produces a platform-specific bundle in `src-tauri/target/release/bundle/`.
**NOTE:** Bundling requires icon files at `src-tauri/icons/{32x32.png,128x128.png,128x128@2x.png,icon.icns,icon.ico}`. Icon generation is part of Sub-plan 5 (Packaging); until then, `tauri build` will fail and only `tauri dev` works.

## Talking to the server

The client expects a `localmail serve` HTTPS endpoint. The connection URL, username, password, and TLS cert pin are stored in the OS keyring — landed in Sub-plan 2.
```

- [ ] **Step 3: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-1
git add gui/README.md
git commit -m "docs(gui-client): README — prereqs + dev/test/build commands"
```

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-05-17-localmail-gui-design.md`):

| Spec section | Sub-plan 1 task |
|---|---|
| Top-level `gui/` directory | Task 1 |
| Tauri 2 + Rust core skeleton | Task 3 |
| Svelte 5 + TypeScript frontend | Tasks 1, 2 |
| Rust + Svelte IPC via `invoke()` | Task 4 |
| Window settings (1280×800, resizable) | Task 3 (tauri.conf.json) |
| `cargo test` for Rust unit tests | Task 3 |
| `vitest` for Svelte component tests | Task 4 |
| `svelte-check` type checking | Task 4 |

**Deferred to later sub-plans** (intentional, not gaps):
- HTTP client (reqwest+rustls) → Sub-plan 2
- Keyring (keyring-rs) → Sub-plan 2
- TOFU cert pin → Sub-plan 2
- TLS bearer auth, login screen → Sub-plan 2
- All UI screens beyond the placeholder greet → Sub-plans 3–4
- Production icons + bundling story → Sub-plan 5

**Placeholder scan:** none — every step has concrete code or commands. The deferred-icons situation in Task 3 step 8 / Task 5 step 1 is explicit and documented.

**Type/name consistency:** `Greeting` struct in Rust (`src-tauri/src/lib.rs` Task 3) has fields `message: String` + `source: &'static str`; TypeScript wrapper (`src/lib/tauri.ts` Task 4) declares the matching interface `Greeting { message: string; source: string }`; Svelte component (`App.svelte` Task 4) consumes them by name. Command name `greet` is consistent across Rust `#[tauri::command]`, TS `invoke("greet", ...)`, and Cargo test name.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-17-localmail-gui-client-1-scaffolding.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between, fast iteration. Best for the 6-task size of this plan.

**2. Inline Execution** — execute in this session via `executing-plans`, batch with checkpoints.

**Which approach?** (Either way, the first action will be Task 0: creating the worktree off `main`.)
