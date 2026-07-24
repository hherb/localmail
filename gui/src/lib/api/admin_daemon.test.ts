import { beforeEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

import {
  getAdminDaemon,
  reloadAdminDaemon,
  restartAccountSync,
  restartAdminDaemon,
  startAdminDaemon,
  stopAdminDaemon,
} from "./admin_daemon";

describe("admin_daemon", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    invokeMock.mockResolvedValue(undefined);
  });

  it("getAdminDaemon invokes get_admin_daemon_cmd", async () => {
    invokeMock.mockResolvedValueOnce({ heartbeats: [], recent_log: [] });
    await getAdminDaemon();
    expect(invokeMock).toHaveBeenCalledWith("get_admin_daemon_cmd");
  });

  it("startAdminDaemon posts the 'start' op", async () => {
    await startAdminDaemon();
    expect(invokeMock).toHaveBeenCalledWith("lifecycle_admin_daemon_cmd", {
      op: "start",
    });
  });

  it("stopAdminDaemon posts the 'stop' op", async () => {
    await stopAdminDaemon();
    expect(invokeMock).toHaveBeenCalledWith("lifecycle_admin_daemon_cmd", {
      op: "stop",
    });
  });

  it("restartAdminDaemon posts the 'restart' op", async () => {
    await restartAdminDaemon();
    expect(invokeMock).toHaveBeenCalledWith("lifecycle_admin_daemon_cmd", {
      op: "restart",
    });
  });

  it("reloadAdminDaemon invokes reload_admin_daemon_cmd", async () => {
    await reloadAdminDaemon();
    expect(invokeMock).toHaveBeenCalledWith("reload_admin_daemon_cmd");
  });

  it("restartAccountSync passes accountId", async () => {
    await restartAccountSync("7");
    expect(invokeMock).toHaveBeenCalledWith("restart_account_sync_cmd", {
      accountId: "7",
    });
  });
});
