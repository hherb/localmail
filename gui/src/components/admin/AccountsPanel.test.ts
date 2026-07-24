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

import AccountsPanel from "./AccountsPanel.svelte";

const ROWS = [
  {
    id: "1",
    name: "gmail",
    email_address: "a@b.c",
    auth_method: "oauth2",
    sync_enabled: true,
  },
  {
    id: "2",
    name: "archive",
    email_address: "old@b.c",
    auth_method: "archive",
    sync_enabled: false,
  },
];

describe("AccountsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listAdminAccounts.mockResolvedValue(ROWS);
    api.updateAdminAccount.mockResolvedValue({});
    api.deleteAdminAccount.mockResolvedValue(undefined);
  });

  it("lists accounts fetched on mount", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="account-row-1"]')).toBeTruthy();
    });
    expect(container.querySelector('[data-testid="account-row-2"]')).toBeTruthy();
    expect(api.listAdminAccounts).toHaveBeenCalledTimes(1);
  });

  it("renders an empty state when there are no accounts", async () => {
    api.listAdminAccounts.mockResolvedValueOnce([]);
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="accounts-empty"]')).toBeTruthy();
    });
  });

  it("surfaces a load failure instead of failing silently", async () => {
    api.listAdminAccounts.mockRejectedValueOnce({
      kind: "Http",
      detail: { kind: "HttpStatus", detail: { status: 403, body: "nope" } },
    });
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      const err = container.querySelector('[data-testid="accounts-error"]');
      expect(err).toBeTruthy();
      expect(err?.textContent).toContain("403");
    });
  });

  it("toggles sync_enabled through updateAdminAccount", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="toggle-sync-1"]')).toBeTruthy();
    });
    const btn = container.querySelector(
      '[data-testid="toggle-sync-1"]',
    ) as HTMLButtonElement;
    await fireEvent.click(btn);
    expect(api.updateAdminAccount).toHaveBeenCalledWith("1", { sync_enabled: false });
  });

  it("deletes without force on the first attempt", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="delete-account-2"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="delete-account-2"]') as HTMLButtonElement,
    );
    expect(api.deleteAdminAccount).toHaveBeenCalledWith("2", false);
  });

  it("opens the create form from the toolbar", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="new-account"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="new-account"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(container.querySelector('[data-testid="account-form"]')).toBeTruthy();
    });
    expect(api.getAdminAccount).not.toHaveBeenCalled();
  });

  it("opens the edit form preloaded from the row", async () => {
    api.getAdminAccount.mockResolvedValueOnce({
      ...ROWS[0],
      oauth_provider: null,
      imap_host: null,
      imap_port: null,
      folder_allow: null,
      folder_deny: null,
      folder_deny_flags: null,
      created_at: "2026-01-01T00:00:00+00:00",
      updated_at: "2026-01-01T00:00:00+00:00",
    });
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="edit-account-1"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="edit-account-1"]') as HTMLButtonElement,
    );
    await waitFor(() => expect(api.getAdminAccount).toHaveBeenCalledWith("1"));
  });

  it("expands the credentials row for a password account", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="open-secrets-1"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="open-secrets-1"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="secrets-test-connection"]'),
      ).toBeTruthy();
    });
  });

  it("offers a force-delete confirmation on 409 and retries with force", async () => {
    api.deleteAdminAccount.mockRejectedValueOnce({
      kind: "Http",
      detail: {
        kind: "HttpStatus",
        detail: { status: 409, body: '{"detail":"account 2 has 1200 messages"}' },
      },
    });
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="delete-account-2"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="delete-account-2"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="confirm-force-delete-2"]'),
      ).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector(
        '[data-testid="confirm-force-delete-2"]',
      ) as HTMLButtonElement,
    );
    expect(api.deleteAdminAccount).toHaveBeenLastCalledWith("2", true);
  });
});
