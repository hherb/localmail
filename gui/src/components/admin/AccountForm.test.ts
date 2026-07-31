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

import AccountForm from "./AccountForm.svelte";

const EXISTING = {
  id: "5",
  name: "gmail",
  email_address: "a@b.c",
  auth_method: "password",
  oauth_provider: null,
  imap_host: "imap.example.com",
  imap_port: 993,
  folder_allow: null,
  folder_deny: null,
  folder_deny_flags: null,
  sync_enabled: true,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

function field(container: Element, id: string): HTMLInputElement {
  return container.querySelector(`[data-testid="${id}"]`) as HTMLInputElement;
}

function submit(container: Element): Promise<boolean> {
  return fireEvent.submit(
    container.querySelector('[data-testid="account-form"]') as HTMLFormElement,
  );
}

describe("AccountForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.createAdminAccount.mockResolvedValue(EXISTING);
    api.updateAdminAccount.mockResolvedValue(EXISTING);
    api.getAdminAccount.mockResolvedValue(EXISTING);
  });

  it("creates a new account from the entered fields", async () => {
    const onSaved = vi.fn();
    const { container } = render(AccountForm, {
      props: { accountId: null, onSaved, onCancel: vi.fn() },
    });
    await fireEvent.input(field(container, "field-name"), {
      target: { value: "work" },
    });
    await fireEvent.input(field(container, "field-email"), {
      target: { value: "w@e.rk" },
    });
    await fireEvent.change(field(container, "field-auth-method"), {
      target: { value: "password" },
    });
    await fireEvent.input(field(container, "field-imap-host"), {
      target: { value: "imap.e.rk" },
    });
    await fireEvent.input(field(container, "field-imap-port"), {
      target: { value: "993" },
    });
    await submit(container);

    await waitFor(() => expect(api.createAdminAccount).toHaveBeenCalledTimes(1));
    expect(api.createAdminAccount).toHaveBeenCalledWith({
      name: "work",
      email_address: "w@e.rk",
      auth_method: "password",
      imap_host: "imap.e.rk",
      imap_port: 993,
    });
    expect(onSaved).toHaveBeenCalledTimes(1);
  });

  it("loads the existing account when editing and patches only changed fields", async () => {
    const onSaved = vi.fn();
    const { container } = render(AccountForm, {
      props: { accountId: "5", onSaved, onCancel: vi.fn() },
    });
    await waitFor(() => expect(field(container, "field-email").value).toBe("a@b.c"));

    await fireEvent.input(field(container, "field-email"), {
      target: { value: "new@b.c" },
    });
    await submit(container);

    await waitFor(() => expect(api.updateAdminAccount).toHaveBeenCalledTimes(1));
    expect(api.updateAdminAccount).toHaveBeenCalledWith("5", {
      email_address: "new@b.c",
    });
  });

  it("renders a server validation error instead of failing silently", async () => {
    api.createAdminAccount.mockRejectedValueOnce({
      kind: "Http",
      detail: {
        kind: "HttpStatus",
        detail: {
          status: 400,
          body: '{"detail":"imap_host is required for live accounts"}',
        },
      },
    });
    const { container } = render(AccountForm, {
      props: { accountId: null, onSaved: vi.fn(), onCancel: vi.fn() },
    });
    await fireEvent.input(field(container, "field-name"), { target: { value: "x" } });
    await fireEvent.input(field(container, "field-email"), {
      target: { value: "x@y.z" },
    });
    await submit(container);
    await waitFor(() => {
      const err = container.querySelector('[data-testid="account-form-error"]');
      expect(err?.textContent).toContain("imap_host is required");
    });
  });

  it("hides the IMAP fields for an archive account", async () => {
    const { container } = render(AccountForm, {
      props: { accountId: null, onSaved: vi.fn(), onCancel: vi.fn() },
    });
    await fireEvent.change(field(container, "field-auth-method"), {
      target: { value: "archive" },
    });
    await waitFor(() => {
      expect(container.querySelector('[data-testid="field-imap-host"]')).toBeFalsy();
    });
  });

  it("calls onCancel without touching the API", async () => {
    const onCancel = vi.fn();
    const { container } = render(AccountForm, {
      props: { accountId: null, onSaved: vi.fn(), onCancel },
    });
    await fireEvent.click(
      container.querySelector('[data-testid="account-form-cancel"]') as HTMLButtonElement,
    );
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(api.createAdminAccount).not.toHaveBeenCalled();
  });

  it("locks the auth method on edit — the transition dead-ends server-side", async () => {
    const { container } = render(AccountForm, {
      props: { accountId: "5", onSaved: vi.fn(), onCancel: vi.fn() },
    });
    await waitFor(() =>
      expect(field(container, "field-auth-method").disabled).toBe(true),
    );
    expect(
      container.querySelector('[data-testid="auth-method-locked"]'),
    ).toBeTruthy();
  });

  it("never creates a new account when an edit's initial load failed", async () => {
    // Regression: submit used to dispatch on `loaded !== null`, so a failed
    // getAdminAccount on an edit fell through to createAdminAccount — silently
    // creating a stray account instead of updating the opened one.
    api.getAdminAccount.mockRejectedValueOnce("load-failed-boom");
    const onSaved = vi.fn();
    const { container } = render(AccountForm, {
      props: { accountId: "5", onSaved, onCancel: vi.fn() },
    });
    await waitFor(() => {
      const err = container.querySelector('[data-testid="account-form-error"]');
      expect(err?.textContent).toContain("load-failed-boom");
    });
    await fireEvent.input(field(container, "field-email"), {
      target: { value: "typed@e.rk" },
    });
    await submit(container);
    await waitFor(() => {
      const err = container.querySelector('[data-testid="account-form-error"]');
      expect(err?.textContent).toContain("finished loading");
    });
    expect(api.createAdminAccount).not.toHaveBeenCalled();
    expect(api.updateAdminAccount).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("rejects a non-numeric port instead of silently dropping it", async () => {
    const { container } = render(AccountForm, {
      props: { accountId: null, onSaved: vi.fn(), onCancel: vi.fn() },
    });
    await fireEvent.input(field(container, "field-name"), { target: { value: "x" } });
    await fireEvent.input(field(container, "field-email"), {
      target: { value: "x@y.z" },
    });
    await fireEvent.input(field(container, "field-imap-host"), {
      target: { value: "imap.e.rk" },
    });
    await fireEvent.input(field(container, "field-imap-port"), {
      target: { value: "not-a-port" },
    });
    await submit(container);
    await waitFor(() => {
      const err = container.querySelector('[data-testid="account-form-error"]');
      expect(err?.textContent).toContain("whole number");
    });
    expect(api.createAdminAccount).not.toHaveBeenCalled();
  });
});
