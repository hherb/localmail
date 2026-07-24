import { fireEvent, render, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listAdminAccounts: vi.fn(),
  updateAdminAccount: vi.fn(),
  deleteAdminAccount: vi.fn(),
  getAdminAccount: vi.fn(),
  createAdminAccount: vi.fn(),
  storeAdminAccountPassword: vi.fn(),
  testAdminAccountConnection: vi.fn(),
}));
vi.mock("../../lib/api/admin_accounts", () => api);

import AccountSecrets from "./AccountSecrets.svelte";

describe("AccountSecrets", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.storeAdminAccountPassword.mockResolvedValue(undefined);
    api.testAdminAccountConnection.mockResolvedValue({
      folders: [{ name: "INBOX", flags: ["\\HasNoChildren"] }],
    });
  });

  it("stores a password and confirms", async () => {
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "password" },
    });
    await fireEvent.input(
      container.querySelector('[data-testid="secrets-password"]') as HTMLInputElement,
      { target: { value: "hunter2" } },
    );
    await fireEvent.click(
      container.querySelector('[data-testid="secrets-store-password"]') as HTMLButtonElement,
    );
    await waitFor(() =>
      expect(api.storeAdminAccountPassword).toHaveBeenCalledWith("4", "hunter2"),
    );
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="secrets-status"]')?.textContent,
      ).toContain("Password stored");
    });
  });

  it("hides the password field for oauth2 accounts", () => {
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "oauth2" },
    });
    expect(container.querySelector('[data-testid="secrets-password"]')).toBeFalsy();
  });

  it("lists the probed folders on a successful test connection", async () => {
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "password" },
    });
    await fireEvent.click(
      container.querySelector('[data-testid="secrets-test-connection"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      const list = container.querySelector('[data-testid="secrets-folders"]');
      expect(list?.textContent).toContain("INBOX");
    });
  });

  it("surfaces a connect failure as an inline error", async () => {
    api.testAdminAccountConnection.mockRejectedValueOnce({
      kind: "Http",
      detail: {
        kind: "HttpStatus",
        detail: {
          status: 400,
          body: '{"detail":"[Errno 8] nodename nor servname provided"}',
        },
      },
    });
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "password" },
    });
    await fireEvent.click(
      container.querySelector('[data-testid="secrets-test-connection"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="secrets-error"]')?.textContent,
      ).toContain("nodename nor servname");
    });
  });

  it("does not offer test-connection for archive accounts", () => {
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "archive" },
    });
    expect(container.querySelector('[data-testid="secrets-test-connection"]')).toBeFalsy();
  });
});
