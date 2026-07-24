import { beforeEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

import {
  createAdminAccount,
  deleteAdminAccount,
  getAdminAccount,
  listAdminAccounts,
  storeAdminAccountPassword,
  testAdminAccountConnection,
  updateAdminAccount,
} from "./admin_accounts";

describe("admin_accounts", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    invokeMock.mockResolvedValue(undefined);
  });

  it("listAdminAccounts invokes list_admin_accounts_cmd", async () => {
    invokeMock.mockResolvedValueOnce([]);
    await listAdminAccounts();
    expect(invokeMock).toHaveBeenCalledWith("list_admin_accounts_cmd");
  });

  it("getAdminAccount passes accountId", async () => {
    await getAdminAccount("3");
    expect(invokeMock).toHaveBeenCalledWith("get_admin_account_cmd", { accountId: "3" });
  });

  it("createAdminAccount passes the input under `input`", async () => {
    const input = {
      name: "n",
      email_address: "a@b.c",
      auth_method: "archive" as const,
    };
    await createAdminAccount(input);
    expect(invokeMock).toHaveBeenCalledWith("create_admin_account_cmd", { input });
  });

  it("updateAdminAccount passes accountId and patch", async () => {
    await updateAdminAccount("3", { sync_enabled: false });
    expect(invokeMock).toHaveBeenCalledWith("update_admin_account_cmd", {
      accountId: "3",
      patch: { sync_enabled: false },
    });
  });

  it("deleteAdminAccount defaults force to false", async () => {
    await deleteAdminAccount("3");
    expect(invokeMock).toHaveBeenCalledWith("delete_admin_account_cmd", {
      accountId: "3",
      force: false,
    });
  });

  it("deleteAdminAccount forwards force=true", async () => {
    await deleteAdminAccount("3", true);
    expect(invokeMock).toHaveBeenCalledWith("delete_admin_account_cmd", {
      accountId: "3",
      force: true,
    });
  });

  it("storeAdminAccountPassword passes the password", async () => {
    await storeAdminAccountPassword("3", "hunter2");
    expect(invokeMock).toHaveBeenCalledWith("store_admin_account_password_cmd", {
      accountId: "3",
      password: "hunter2",
    });
  });

  it("testAdminAccountConnection passes accountId", async () => {
    invokeMock.mockResolvedValueOnce({ folders: [] });
    await testAdminAccountConnection("3");
    expect(invokeMock).toHaveBeenCalledWith("test_admin_account_connection_cmd", {
      accountId: "3",
    });
  });
});
