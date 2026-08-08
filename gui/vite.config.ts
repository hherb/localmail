import { readFileSync } from "node:fs";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { svelteTesting } from "@testing-library/svelte/vite";

// Tauri expects a fixed port; fail if it can't bind.
const host = process.env.TAURI_DEV_HOST;

// The client version the About tab shows, injected from `package.json` rather
// than written out again in a component. It had been a hand-kept literal there,
// and had drifted three minors ahead of both GUI manifests before anything
// compared the two. `package.json` is itself pinned to `pyproject.toml` by
// tests/test_version_single_source.py, so the whole chain has one source.
const appVersion = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf-8"),
).version;

export default defineConfig(async () => ({
  plugins: [svelte(), svelteTesting()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
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
