import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

// AccountsPanel fetches on mount, so the admin API module is stubbed here.
const api = vi.hoisted(() => ({
  listAdminAccounts: vi.fn(async () => []),
  updateAdminAccount: vi.fn(),
  deleteAdminAccount: vi.fn(),
  getAdminAccount: vi.fn(),
  createAdminAccount: vi.fn(),
  storeAdminAccountPassword: vi.fn(),
  testAdminAccountConnection: vi.fn(),
}));
vi.mock("../lib/api/admin_accounts", () => api);

import AdminView from "./AdminView.svelte";

describe("AdminView", () => {
  it("renders nothing when closed", () => {
    const { container } = render(AdminView, {
      props: { open: false, onClose: vi.fn() },
    });
    expect(container.querySelector('[role="dialog"]')).toBeFalsy();
  });

  it("renders four tabs when open, accounts selected first", () => {
    const { container } = render(AdminView, {
      props: { open: true, onClose: vi.fn() },
    });
    expect(container.querySelectorAll('[role="tab"]').length).toBe(4);
    const accounts = container.querySelector('[data-testid="admin-tab-accounts"]');
    expect(accounts?.getAttribute("aria-selected")).toBe("true");
  });

  it("switches the active tab on click", async () => {
    const { container } = render(AdminView, {
      props: { open: true, onClose: vi.fn() },
    });
    const daemon = container.querySelector(
      '[data-testid="admin-tab-daemon"]',
    ) as HTMLButtonElement;
    await fireEvent.click(daemon);
    expect(daemon.getAttribute("aria-selected")).toBe("true");
    expect(
      container
        .querySelector('[data-testid="admin-tab-accounts"]')
        ?.getAttribute("aria-selected"),
    ).toBe("false");
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const { container } = render(AdminView, { props: { open: true, onClose } });
    const close = container.querySelector(".close") as HTMLButtonElement;
    await fireEvent.click(close);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
