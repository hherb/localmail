import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render } from "@testing-library/svelte";

// jsdom in this project's vitest setup does not provide window.localStorage,
// so install a minimal in-memory shim before any store module that reads it
// is imported.
function installLocalStorageShim(): Storage {
  const map = new Map<string, string>();
  const shim: Storage = {
    get length() { return map.size; },
    clear() { map.clear(); },
    getItem(key: string): string | null { return map.has(key) ? (map.get(key) as string) : null; },
    key(index: number): string | null { return Array.from(map.keys())[index] ?? null; },
    removeItem(key: string): void { map.delete(key); },
    setItem(key: string, value: string): void { map.set(key, String(value)); },
  };
  Object.defineProperty(window, "localStorage", {
    value: shim,
    configurable: true,
    writable: true,
  });
  Object.defineProperty(globalThis, "localStorage", {
    value: shim,
    configurable: true,
    writable: true,
  });
  return shim;
}

installLocalStorageShim();

const tauriMocks = vi.hoisted(() => ({
  listAccounts: vi.fn(async () => []),
  listFolders: vi.fn(async () => []),
  listRecentMessages: vi.fn(async () => ({
    new_messages: [],
    next_cursor: null,
  })),
  listMessages: vi.fn(async () => ({
    messages: [],
    next_cursor: null,
  })),
  getMessage: vi.fn(),
  runSearch: vi.fn(async () => ({
    results: [],
    next_cursor: null,
    total_estimate: null,
    took_ms: 0,
  })),
}));

vi.mock("../lib/tauri", () => tauriMocks);

const apiMocks = vi.hoisted(() => ({
  getChanges: vi.fn(async () => ({ new_messages: [], next_cursor: null })),
  getVersion: vi.fn(async () => ({
    api_major: 1,
    api_minor: 0,
    server_version: null,
    build_hash: null,
  })),
}));

vi.mock("../lib/api/changes", () => ({ getChanges: apiMocks.getChanges }));
vi.mock("../lib/api/version", () => ({ getVersion: apiMocks.getVersion }));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async () => undefined),
}));

import MainView from "./MainView.svelte";
import { auth } from "../lib/stores/auth.svelte";
import { mail } from "../lib/stores/mail.svelte";
import { version } from "../lib/stores/version.svelte";

function forceLoggedIn(): void {
  Object.assign(auth.snapshot, {
    phase: "logged_in",
    username: "test-user",
    capabilities: {
      search: false,
      attachments: false,
      attachment_text: false,
      threading: false,
      send: false,
    },
  });
}

beforeEach(() => {
  mail.reset();
  auth.reset();
  version.reset();
  vi.clearAllMocks();
  window.localStorage.clear();
});

afterEach(() => {
  mail.stopPolling();
});

describe("MainView", () => {
  it("renders nothing when not logged in", () => {
    const { container } = render(MainView);
    // VersionGate is mounted unconditionally, but the .app shell only
    // appears when phase === "logged_in".
    expect(container.querySelector(".app")).toBeFalsy();
  });

  it("renders the three-pane shell with two splitters when logged in", () => {
    forceLoggedIn();
    const { container } = render(MainView);
    expect(container.querySelector(".app")).toBeTruthy();
    expect(container.querySelector(".panes")).toBeTruthy();
    const splitters = container.querySelectorAll(".splitter");
    expect(splitters.length).toBe(2);
  });

  it("applies persisted pane widths via inline grid-template-columns", () => {
    window.localStorage.setItem(
      "localmail.gui.paneWidths",
      JSON.stringify({ left: 280, middle: 420 }),
    );
    forceLoggedIn();
    const { container } = render(MainView);
    const panes = container.querySelector(".panes") as HTMLElement | null;
    expect(panes).toBeTruthy();
    const style = panes!.getAttribute("style") ?? "";
    expect(style).toContain("280px");
    expect(style).toContain("420px");
  });

  it("falls back to defaults when localStorage is empty", () => {
    forceLoggedIn();
    const { container } = render(MainView);
    const panes = container.querySelector(".panes") as HTMLElement | null;
    const style = panes!.getAttribute("style") ?? "";
    // DEFAULT_LEFT_WIDTH_PX = 220, DEFAULT_MIDDLE_WIDTH_PX = 340 (from splitter.ts).
    expect(style).toContain("220px");
    expect(style).toContain("340px");
  });

  it("Settings button opens the SettingsScreen overlay and × closes it", async () => {
    forceLoggedIn();
    const { container, getByTestId, getByLabelText } = render(MainView);
    expect(container.querySelector('[role="dialog"]')).toBeFalsy();
    await fireEvent.click(getByTestId("open-settings"));
    expect(container.querySelector('[role="dialog"]')).toBeTruthy();
    await fireEvent.click(getByLabelText(/^close$/i));
    expect(container.querySelector('[role="dialog"]')).toBeFalsy();
  });

  it("does not load accounts/messages or start polling when the server's api_major is incompatible", async () => {
    // The hard gate: a VersionGate overlay alone is not enough — if data
    // loads and the 30s poller still run, we hammer an incompatible server
    // and risk misinterpreting payloads. Verify both are suppressed.
    apiMocks.getVersion.mockResolvedValueOnce({
      api_major: 999,
      api_minor: 0,
      server_version: null,
      build_hash: null,
    });
    forceLoggedIn();
    render(MainView);
    // Let MainView's async onMount settle: it awaits version.check() then
    // checks compatible before any further IO. A single microtask flush
    // isn't enough because of the chained await; spin twice.
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(version.snapshot.compatible).toBe(false);
    expect(tauriMocks.listAccounts).not.toHaveBeenCalled();
    expect(tauriMocks.listMessages).not.toHaveBeenCalled();
    expect(mail.isPolling).toBe(false);
  });

  it("loads accounts/messages and starts polling on api_major match", async () => {
    apiMocks.getVersion.mockResolvedValueOnce({
      api_major: 1,
      api_minor: 0,
      server_version: null,
      build_hash: null,
    });
    forceLoggedIn();
    render(MainView);
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(version.snapshot.compatible).toBe(true);
    expect(tauriMocks.listAccounts).toHaveBeenCalled();
    expect(tauriMocks.listMessages).toHaveBeenCalled();
    expect(mail.isPolling).toBe(true);
  });
});
