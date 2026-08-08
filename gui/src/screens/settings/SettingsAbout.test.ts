import { render, fireEvent } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

import SettingsAbout from "./SettingsAbout.svelte";
import { version } from "../../lib/stores/version.svelte";

beforeEach(() => {
  version.reset();
  invokeMock.mockReset();
});

afterEach(() => {
  version.reset();
});

describe("SettingsAbout", () => {
  it("renders version info from the version store", () => {
    Object.assign(version.snapshot, {
      info: {
        api_major: 1,
        api_minor: 0,
        server_version: "9.9.9",
        build_hash: "abc123",
      },
      compatible: true,
    });
    const { getByText, container } = render(SettingsAbout);
    expect(getByText("9.9.9")).toBeTruthy();
    expect(getByText("abc123")).toBeTruthy();
    // Client version comes from vite's `define`, sourced from package.json —
    // asserting a literal here is what let the old constant drift unnoticed.
    expect(container.textContent ?? "").toContain(__APP_VERSION__);
  });

  it("invokes open_logs_cmd when the log-directory button is clicked", async () => {
    invokeMock.mockResolvedValue(undefined);
    const { getByRole } = render(SettingsAbout);
    await fireEvent.click(getByRole("button", { name: /open log directory/i }));
    expect(invokeMock).toHaveBeenCalledWith("open_logs_cmd");
  });
});
