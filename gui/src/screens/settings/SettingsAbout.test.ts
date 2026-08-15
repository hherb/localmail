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

  it("explains an absent build rather than showing a bare placeholder", () => {
    Object.assign(version.snapshot, {
      info: {
        api_major: 1,
        api_minor: 0,
        server_version: "0.3.0",
        build_hash: null,
        build_source: "not_a_repo",
        version_source: "installed",
      },
      compatible: true,
    });

    const { getByText } = render(SettingsAbout);

    expect(getByText("— not a repository")).toBeTruthy();
  });

  it("marks the server row when its version could not be resolved", () => {
    Object.assign(version.snapshot, {
      info: {
        api_major: 1,
        api_minor: 0,
        server_version: "0.0.0+unknown",
        build_hash: "eec8e09",
        build_source: "git_checkout",
        version_source: "metadata_unreadable",
      },
      compatible: true,
    });

    const { getByText } = render(SettingsAbout);

    // #300: the sentinel used to render as though it were a version.
    expect(getByText("(metadata unreadable)")).toBeTruthy();
  });

  it("shows no marker for a healthy install", () => {
    Object.assign(version.snapshot, {
      info: {
        api_major: 1,
        api_minor: 0,
        server_version: "0.3.0",
        build_hash: "eec8e09",
        build_source: "git_checkout",
        version_source: "installed",
      },
      compatible: true,
    });

    const { queryByText } = render(SettingsAbout);

    // The positive control for the two above: a rule that always marked the row
    // would satisfy them both and cry wolf on every healthy install.
    expect(queryByText(/\(.*unreadable.*\)/)).toBeNull();
    expect(queryByText("eec8e09")).toBeTruthy();
  });
});
