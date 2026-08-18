import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  probeMock: vi.fn(),
  confirmTrustMock: vi.fn(),
  loginMock: vi.fn(),
  logoutMock: vi.fn(),
  whoamiMock: vi.fn(),
  getCapabilitiesMock: vi.fn(),
  getConnectionInfoMock: vi.fn(),
  refreshMock: vi.fn(),
}));

vi.mock("../tauri", () => ({
  probeServer: mocks.probeMock,
  confirmTrust: mocks.confirmTrustMock,
  login: mocks.loginMock,
  logout: mocks.logoutMock,
  refresh: mocks.refreshMock,
  whoami: mocks.whoamiMock,
  getCapabilities: mocks.getCapabilitiesMock,
  getConnectionInfo: mocks.getConnectionInfoMock,
}));

import { auth } from "./auth.svelte";

describe("auth store", () => {
  beforeEach(() => {
    mocks.probeMock.mockReset();
    mocks.confirmTrustMock.mockReset();
    mocks.loginMock.mockReset();
    mocks.logoutMock.mockReset();
    mocks.refreshMock.mockReset();
    mocks.whoamiMock.mockReset();
    mocks.getCapabilitiesMock.mockReset();
    mocks.getConnectionInfoMock.mockReset();
    mocks.getConnectionInfoMock.mockResolvedValue({
      server_url: "https://localhost:8443/",
      cert_sha256_pin: "deadbeef",
    });
    auth.reset();
  });

  it("starts in 'connecting' state", () => {
    expect(auth.snapshot.phase).toBe("connecting");
  });

  it("refreshState moves to logged_out when whoami throws NotLoggedIn", async () => {
    mocks.whoamiMock.mockRejectedValueOnce({ kind: "NotLoggedIn" });
    await auth.refreshState();
    expect(auth.snapshot.phase).toBe("logged_out");
  });

  it("refreshState moves to logged_in when whoami returns a user", async () => {
    mocks.whoamiMock.mockResolvedValueOnce({ username: "alice", user_id: "1" });
    mocks.getCapabilitiesMock.mockResolvedValueOnce({
      search: true, attachments: true, attachment_text: true,
      threading: false, send: false,
    });
    await auth.refreshState();
    expect(auth.snapshot.phase).toBe("logged_in");
    if (auth.snapshot.phase === "logged_in") {
      expect(auth.snapshot.username).toBe("alice");
      expect(auth.snapshot.capabilities.search).toBe(true);
    }
    expect(auth.serverUrl).toBe("https://localhost:8443/");
    expect(auth.certPin).toBe("deadbeef");
  });

  it("probe stores result in needs_trust state", async () => {
    mocks.probeMock.mockResolvedValueOnce({
      api_major: 1, api_minor: 0,
      server_version: "0.1.0",
      cert_sha256: "abc123",
    });
    await auth.probe("https://localhost:8443");
    expect(auth.snapshot.phase).toBe("needs_trust");
    if (auth.snapshot.phase === "needs_trust") {
      expect(auth.snapshot.certSha256).toBe("abc123");
      expect(auth.snapshot.url).toBe("https://localhost:8443");
    }
  });

  it("confirmTrust calls Rust and moves to logged_out", async () => {
    mocks.probeMock.mockResolvedValueOnce({
      api_major: 1, api_minor: 0,
      server_version: "0.1.0",
      cert_sha256: "abc",
    });
    await auth.probe("https://localhost:8443");
    mocks.confirmTrustMock.mockResolvedValueOnce(undefined);
    await auth.confirmTrust();
    expect(mocks.confirmTrustMock).toHaveBeenCalledWith("https://localhost:8443", "abc");
    expect(auth.snapshot.phase).toBe("logged_out");
  });

  it("login moves to logged_in via whoami + capabilities", async () => {
    mocks.loginMock.mockResolvedValueOnce({ username: "alice", expires_at: "2026-12-01T00:00:00Z" });
    mocks.whoamiMock.mockResolvedValueOnce({ username: "alice", user_id: "1" });
    mocks.getCapabilitiesMock.mockResolvedValueOnce({
      search: true, attachments: true, attachment_text: true,
      threading: false, send: false,
    });
    await auth.login("alice", "hunter2");
    expect(auth.snapshot.phase).toBe("logged_in");
  });

  it("login failure leaves us in logged_out with errorMessage", async () => {
    mocks.loginMock.mockRejectedValueOnce({ kind: "Http", detail: "401: invalid" });
    await auth.login("alice", "wrong");
    expect(auth.snapshot.phase).toBe("logged_out");
    if (auth.snapshot.phase === "logged_out") {
      expect(auth.snapshot.errorMessage).toContain("401");
    }
  });

  it("logout clears state to logged_out", async () => {
    mocks.loginMock.mockResolvedValueOnce({ username: "alice", expires_at: "x" });
    mocks.whoamiMock.mockResolvedValueOnce({ username: "alice", user_id: "1" });
    mocks.getCapabilitiesMock.mockResolvedValueOnce({
      search: true, attachments: true, attachment_text: true,
      threading: false, send: false,
    });
    await auth.login("alice", "hunter2");
    expect(auth.snapshot.phase).toBe("logged_in");
    mocks.logoutMock.mockResolvedValueOnce(undefined);
    await auth.logout();
    expect(mocks.logoutMock).toHaveBeenCalled();
    expect(auth.snapshot.phase).toBe("logged_out");
  });

  it("changeServer logs out and returns to the connect phase", async () => {
    mocks.logoutMock.mockResolvedValueOnce(undefined);
    await auth.changeServer();
    expect(mocks.logoutMock).toHaveBeenCalled();
    expect(auth.snapshot.phase).toBe("connecting");
  });

  it("carries is_admin from whoami into the logged_in snapshot", async () => {
    mocks.whoamiMock.mockResolvedValueOnce({
      username: "root", user_id: "1", is_admin: true,
    });
    mocks.getCapabilitiesMock.mockResolvedValueOnce({
      search: true, attachments: true, attachment_text: true,
      threading: false, send: false,
    });
    await auth.refreshState();
    const snap = auth.snapshot;
    expect(snap.phase).toBe("logged_in");
    expect(snap.phase === "logged_in" && snap.isAdmin).toBe(true);
  });

  it("defaults isAdmin to false when whoami omits it", async () => {
    mocks.whoamiMock.mockResolvedValueOnce({ username: "viewer", user_id: "7" });
    mocks.getCapabilitiesMock.mockResolvedValueOnce({
      search: true, attachments: true, attachment_text: true,
      threading: false, send: false,
    });
    await auth.refreshState();
    const snap = auth.snapshot;
    expect(snap.phase === "logged_in" && snap.isAdmin).toBe(false);
  });
});
