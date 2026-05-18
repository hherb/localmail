import { render } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Hoisted mocks so the vi.mock factories can reference shared state safely.
const mocks = vi.hoisted(() => ({
  getVersionMock: vi.fn(),
  invokeMock: vi.fn(async () => undefined),
}));

vi.mock("../lib/api/version", () => ({
  getVersion: mocks.getVersionMock,
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: mocks.invokeMock,
}));

import VersionGate from "./VersionGate.svelte";
import { version } from "../lib/stores/version.svelte";

// Allow microtasks queued by onMount -> version.check() to settle so the
// component re-renders against the post-check store state.
async function flush(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
}

beforeEach(() => {
  mocks.getVersionMock.mockReset();
  mocks.invokeMock.mockReset();
  version.reset();
});

afterEach(() => {
  version.reset();
});

describe("VersionGate", () => {
  it("renders nothing when compatible is null (check still pending or failed)", async () => {
    // getVersion rejects -> compatible stays null, errorMessage set.
    mocks.getVersionMock.mockRejectedValueOnce(new Error("nope"));
    const { container } = render(VersionGate);
    await flush();
    expect(container.querySelector("[role=dialog]")).toBeFalsy();
    expect(version.snapshot.compatible).toBeNull();
  });

  it("renders nothing when compatible=true", async () => {
    mocks.getVersionMock.mockResolvedValueOnce({
      api_major: 1,
      api_minor: 0,
      server_version: null,
      build_hash: null,
    });
    const { container } = render(VersionGate);
    await flush();
    expect(version.snapshot.compatible).toBe(true);
    expect(container.querySelector("[role=dialog]")).toBeFalsy();
  });

  it("renders the dialog when compatible=false", async () => {
    mocks.getVersionMock.mockResolvedValueOnce({
      api_major: 2,
      api_minor: 0,
      server_version: null,
      build_hash: null,
    });
    const { container } = render(VersionGate);
    await flush();
    expect(version.snapshot.compatible).toBe(false);
    const dialog = container.querySelector("[role=dialog]");
    expect(dialog).toBeTruthy();
    // The modal body surfaces the server's reported api_major.
    expect(dialog?.textContent).toContain("2");
    expect(dialog?.textContent).toContain("Incompatible server");
    expect(container.querySelector("button")?.textContent).toContain("Quit");
  });

  it("clicking Quit invokes quit_app_cmd", async () => {
    mocks.getVersionMock.mockResolvedValueOnce({
      api_major: 2,
      api_minor: 0,
      server_version: null,
      build_hash: null,
    });
    const { container } = render(VersionGate);
    await flush();
    const button = container.querySelector("button") as HTMLButtonElement;
    expect(button).toBeTruthy();
    button.click();
    await flush();
    expect(mocks.invokeMock).toHaveBeenCalledWith("quit_app_cmd");
  });
});
