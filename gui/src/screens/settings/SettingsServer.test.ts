import { fireEvent, render } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the Tauri-backed change-password module so the form submission
// doesn't actually try to invoke a Rust command.
const changePasswordMock = vi.hoisted(() => vi.fn(async () => undefined));
vi.mock("../../lib/api/change_password", () => ({
  changePassword: changePasswordMock,
}));

// Mock the full Tauri bridge so the auth store can be exercised without
// hitting the real OS keyring or any HTTPS endpoint.
const logoutRustMock = vi.hoisted(() => vi.fn(async () => undefined));
vi.mock("../../lib/tauri", () => ({
  probeServer: vi.fn(),
  confirmTrust: vi.fn(),
  login: vi.fn(),
  logout: logoutRustMock,
  refresh: vi.fn(),
  whoami: vi.fn(),
  getCapabilities: vi.fn(),
}));

import SettingsServer from "./SettingsServer.svelte";
import { auth } from "../../lib/stores/auth.svelte";

describe("SettingsServer", () => {
  beforeEach(() => {
    auth.reset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("calls changePassword with the form values on submit", async () => {
    const { container } = render(SettingsServer);
    const oldInput = container.querySelector(
      '[data-testid="old-password"]',
    ) as HTMLInputElement;
    const newInput = container.querySelector(
      '[data-testid="new-password"]',
    ) as HTMLInputElement;
    expect(oldInput).toBeTruthy();
    expect(newInput).toBeTruthy();

    await fireEvent.input(oldInput, { target: { value: "old-pw" } });
    await fireEvent.input(newInput, { target: { value: "new-pw" } });
    const submitBtn = container.querySelector(
      '[data-testid="change-password-submit"]',
    ) as HTMLButtonElement;
    await fireEvent.click(submitBtn);

    expect(changePasswordMock).toHaveBeenCalledTimes(1);
    expect(changePasswordMock).toHaveBeenCalledWith("old-pw", "new-pw");
  });

  it("renders the success message after a successful change", async () => {
    const { container } = render(SettingsServer);
    const oldInput = container.querySelector(
      '[data-testid="old-password"]',
    ) as HTMLInputElement;
    const newInput = container.querySelector(
      '[data-testid="new-password"]',
    ) as HTMLInputElement;

    await fireEvent.input(oldInput, { target: { value: "a" } });
    await fireEvent.input(newInput, { target: { value: "b" } });
    const submitBtn = container.querySelector(
      '[data-testid="change-password-submit"]',
    ) as HTMLButtonElement;
    await fireEvent.click(submitBtn);

    // microtask flush so the post-await branch runs
    await Promise.resolve();
    await Promise.resolve();

    const msg = container.querySelector(
      '[data-testid="change-password-message"]',
    );
    expect(msg).toBeTruthy();
    expect(msg?.textContent).toContain("Password changed");
  });

  it("clicking Log out invokes auth.logout (which calls the Rust logout cmd)", async () => {
    const { container } = render(SettingsServer);
    const btn = container.querySelector(
      '[data-testid="logout-button"]',
    ) as HTMLButtonElement;
    expect(btn).toBeTruthy();
    await fireEvent.click(btn);
    // The auth store's logout() is the documented integration point; it
    // delegates to the mocked Rust `logout`.
    expect(logoutRustMock).toHaveBeenCalledTimes(1);
  });

  it("clicking Re-trust cert reveals the current pin (non-destructive)", async () => {
    const { container } = render(SettingsServer);
    // Before click — message should not exist yet.
    expect(
      container.querySelector('[data-testid="retrust-message"]'),
    ).toBeFalsy();
    const btn = container.querySelector(
      '[data-testid="retrust-button"]',
    ) as HTMLButtonElement;
    expect(btn).toBeTruthy();
    await fireEvent.click(btn);
    const msg = container.querySelector('[data-testid="retrust-message"]');
    expect(msg).toBeTruthy();
    expect(msg?.textContent).toContain("Current pin");
  });

  it("renders fallback strings when not connected / logged out", () => {
    const { container } = render(SettingsServer);
    const url = container.querySelector('[data-testid="server-url"]');
    const user = container.querySelector('[data-testid="server-username"]');
    const pin = container.querySelector('[data-testid="server-cert-pin"]');
    expect(url?.textContent).toContain("(not connected)");
    expect(user?.textContent).toContain("(logged out)");
    expect(pin?.textContent).toContain("(unknown)");
  });
});
