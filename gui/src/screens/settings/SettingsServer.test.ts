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
const probeServerMock = vi.hoisted(() => vi.fn());
vi.mock("../../lib/tauri", () => ({
  probeServer: probeServerMock,
  confirmTrust: vi.fn(),
  login: vi.fn(),
  logout: logoutRustMock,
  refresh: vi.fn(),
  whoami: vi.fn(),
  getCapabilities: vi.fn(),
  getConnectionInfo: vi.fn(),
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listMessages: vi.fn(),
  getMessage: vi.fn(),
  runSearch: vi.fn(),
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

  it("re-runs the secure probe when verifying the certificate again", async () => {
    probeServerMock.mockResolvedValue({
      api_major: 1,
      api_minor: 0,
      server_version: "0.3.0",
      cert_sha256: "deadbeef",
    });
    await auth.probe("https://localhost:8443");
    await auth.confirmTrust();
    const { container } = render(SettingsServer);
    const btn = container.querySelector(
      '[data-testid="retrust-button"]',
    ) as HTMLButtonElement;
    expect(btn).toBeTruthy();
    await fireEvent.click(btn);
    expect(probeServerMock).toHaveBeenLastCalledWith("https://localhost:8443");
    expect(auth.snapshot.phase).toBe("needs_trust");
  });

  it("Change server logs out and returns to connection setup", async () => {
    const { container } = render(SettingsServer);
    await fireEvent.click(
      container.querySelector('[data-testid="change-server-button"]') as HTMLButtonElement,
    );
    expect(logoutRustMock).toHaveBeenCalled();
    expect(auth.snapshot.phase).toBe("connecting");
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

  it("clears both password fields after a successful change", async () => {
    const { container } = render(SettingsServer);
    const oldInput = container.querySelector('[data-testid="old-password"]') as HTMLInputElement;
    const newInput = container.querySelector('[data-testid="new-password"]') as HTMLInputElement;
    await fireEvent.input(oldInput, { target: { value: "a" } });
    await fireEvent.input(newInput, { target: { value: "b" } });
    await fireEvent.click(container.querySelector('[data-testid="change-password-submit"]') as HTMLButtonElement);
    await Promise.resolve();
    await Promise.resolve();
    expect(oldInput.value).toBe("");
    expect(newInput.value).toBe("");
  });

  it("on 401 (wrong current password) clears only old-password and shows a friendly message", async () => {
    // The Rust side returns a structured AuthError; we round-trip the
    // HttpError::HttpStatus { status: 401 } shape the GUI now branches on.
    changePasswordMock.mockRejectedValueOnce({
      kind: "Http",
      detail: { kind: "HttpStatus", detail: { status: 401, body: "wrong" } },
    });
    const { container } = render(SettingsServer);
    const oldInput = container.querySelector('[data-testid="old-password"]') as HTMLInputElement;
    const newInput = container.querySelector('[data-testid="new-password"]') as HTMLInputElement;
    await fireEvent.input(oldInput, { target: { value: "wrong-old" } });
    await fireEvent.input(newInput, { target: { value: "new" } });
    await fireEvent.click(container.querySelector('[data-testid="change-password-submit"]') as HTMLButtonElement);
    await Promise.resolve();
    await Promise.resolve();
    expect(oldInput.value).toBe("");
    expect(newInput.value).toBe("new");
    const msg = container.querySelector('[data-testid="change-password-message"]');
    expect(msg?.textContent).toContain("Current password is incorrect");
  });

  it("on transient (non-401) error keeps both fields so the user does not have to retype", async () => {
    // change_password returns AuthError; after issue #22 there is no Io
    // variant — a transient error surfaces as a nested HttpError chain.
    changePasswordMock.mockRejectedValueOnce({
      kind: "Http",
      detail: { kind: "Network", detail: "timeout" },
    });
    const { container } = render(SettingsServer);
    const oldInput = container.querySelector('[data-testid="old-password"]') as HTMLInputElement;
    const newInput = container.querySelector('[data-testid="new-password"]') as HTMLInputElement;
    await fireEvent.input(oldInput, { target: { value: "old" } });
    await fireEvent.input(newInput, { target: { value: "new" } });
    await fireEvent.click(container.querySelector('[data-testid="change-password-submit"]') as HTMLButtonElement);
    await Promise.resolve();
    await Promise.resolve();
    expect(oldInput.value).toBe("old");
    expect(newInput.value).toBe("new");
  });
});
