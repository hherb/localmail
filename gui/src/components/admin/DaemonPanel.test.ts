import { fireEvent, render, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getAdminDaemon: vi.fn(),
  startAdminDaemon: vi.fn(),
  stopAdminDaemon: vi.fn(),
  restartAdminDaemon: vi.fn(),
  reloadAdminDaemon: vi.fn(),
  restartAccountSync: vi.fn(),
}));
vi.mock("../../lib/api/admin_daemon", () => api);

import DaemonPanel from "./DaemonPanel.svelte";

function view(overrides: Record<string, unknown> = {}) {
  return {
    state: "running",
    pid: 4242,
    started_at: "2026-07-24T00:00:00+00:00",
    supervise_daemon_externally: false,
    heartbeats: [
      {
        worker_kind: "idle",
        account_id: "3",
        state: "idle",
        current_folder: "INBOX",
        last_error_msg: null,
        started_at: "2026-07-24T00:00:00+00:00",
        last_heartbeat_at: "2026-07-24T00:00:05+00:00",
        stale: false,
      },
      {
        worker_kind: "poll",
        account_id: "3",
        state: "polling",
        current_folder: null,
        last_error_msg: "boom",
        started_at: "2026-07-24T00:00:00+00:00",
        last_heartbeat_at: "2026-07-24T00:00:01+00:00",
        stale: true,
      },
    ],
    recent_log: ["line one", "line two"],
    ...overrides,
  };
}

const CONFLICT = {
  kind: "Http",
  detail: { kind: "HttpStatus", detail: { status: 409, body: "busy" } },
};

describe("DaemonPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getAdminDaemon.mockResolvedValue(view());
    api.startAdminDaemon.mockResolvedValue({ state: "starting", pid: null, started_at: null });
    api.stopAdminDaemon.mockResolvedValue({ state: "stopping", pid: null, started_at: null });
    api.restartAdminDaemon.mockResolvedValue({ state: "stopping", pid: null, started_at: null });
    api.reloadAdminDaemon.mockResolvedValue({ command_id: "1" });
    api.restartAccountSync.mockResolvedValue({ command_id: "2" });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows process state, heartbeats and recent log fetched on mount", async () => {
    const { container, getByTestId } = render(DaemonPanel);
    await waitFor(() => {
      expect(getByTestId("daemon-state").textContent).toContain("running");
    });
    expect(container.querySelector('[data-testid="heartbeat-row-0"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="heartbeat-row-1"]')).toBeTruthy();
    expect(getByTestId("daemon-log").textContent).toContain("line one");
    expect(getByTestId("daemon-log").textContent).toContain("line two");
    expect(api.getAdminDaemon).toHaveBeenCalled();
  });

  it("marks only the stale heartbeat row red via the server flag", async () => {
    const { getByTestId } = render(DaemonPanel);
    await waitFor(() => getByTestId("heartbeat-row-1"));
    expect(getByTestId("heartbeat-row-0").className).not.toContain("daemon-stale");
    expect(getByTestId("heartbeat-row-1").className).toContain("daemon-stale");
  });

  it("disables lifecycle buttons but keeps reload/restart-sync enabled when externally supervised", async () => {
    api.getAdminDaemon.mockResolvedValueOnce(
      view({ state: "external", pid: null, started_at: null, supervise_daemon_externally: true }),
    );
    const { getByTestId } = render(DaemonPanel);
    await waitFor(() => getByTestId("daemon-external-note"));
    expect((getByTestId("daemon-start") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("daemon-stop") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("daemon-restart") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("daemon-reload") as HTMLButtonElement).disabled).toBe(false);
    expect((getByTestId("restart-sync-3") as HTMLButtonElement).disabled).toBe(false);
  });

  it("enables lifecycle buttons when the daemon is supervised in-process", async () => {
    const { getByTestId } = render(DaemonPanel);
    await waitFor(() => getByTestId("daemon-start"));
    expect((getByTestId("daemon-start") as HTMLButtonElement).disabled).toBe(false);
    expect(container_hasNote(getByTestId)).toBe(false);
  });

  it("renders exactly one restart-sync button per account (idle+poll deduped)", async () => {
    const { container } = render(DaemonPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="restart-sync-3"]')).toBeTruthy();
    });
    expect(container.querySelectorAll('[data-testid^="restart-sync-"]').length).toBe(1);
  });

  it("reloads via reloadAdminDaemon and reports acceptance", async () => {
    const { getByTestId } = render(DaemonPanel);
    await waitFor(() => getByTestId("daemon-reload"));
    await fireEvent.click(getByTestId("daemon-reload"));
    expect(api.reloadAdminDaemon).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(getByTestId("daemon-action-message").textContent).toBeTruthy();
    });
  });

  it("restarts a single account's sync with its id", async () => {
    const { getByTestId } = render(DaemonPanel);
    await waitFor(() => getByTestId("restart-sync-3"));
    await fireEvent.click(getByTestId("restart-sync-3"));
    expect(api.restartAccountSync).toHaveBeenCalledWith("3");
  });

  it("surfaces a busy-guard 409 as a visible message, not an inert button", async () => {
    api.startAdminDaemon.mockRejectedValueOnce(CONFLICT);
    const { getByTestId } = render(DaemonPanel);
    await waitFor(() => getByTestId("daemon-start"));
    await fireEvent.click(getByTestId("daemon-start"));
    await waitFor(() => {
      const msg = getByTestId("daemon-action-message");
      expect(msg.textContent?.toLowerCase()).toContain("progress");
    });
  });

  it("surfaces a load failure instead of failing silently", async () => {
    api.getAdminDaemon.mockRejectedValueOnce({
      kind: "Http",
      detail: { kind: "HttpStatus", detail: { status: 403, body: "nope" } },
    });
    const { getByTestId } = render(DaemonPanel);
    await waitFor(() => {
      expect(getByTestId("daemon-error").textContent).toContain("403");
    });
  });

  it("shows the error state when the command resolves to a non-object", async () => {
    api.getAdminDaemon.mockResolvedValueOnce(undefined);
    const { container } = render(DaemonPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="daemon-error"]')).toBeTruthy();
    });
  });

  it("does not keep polling after unmount during the initial fetch", async () => {
    // If the panel is unmounted while the very first getAdminDaemon() is still
    // in flight, onDestroy runs before the poll interval is assigned. The
    // interval must not be started onto the dead component afterwards, or it
    // polls the server forever. See DaemonPanel.svelte onMount/onDestroy.
    vi.useFakeTimers();
    let resolveFetch: (v: unknown) => void = () => {};
    api.getAdminDaemon.mockReturnValueOnce(
      new Promise((res) => {
        resolveFetch = res;
      }),
    );

    const { unmount } = render(DaemonPanel);
    await vi.advanceTimersByTimeAsync(0); // let onMount start the (pending) fetch
    expect(api.getAdminDaemon).toHaveBeenCalledTimes(1);

    unmount(); // onDestroy: pollTimer still null, nothing to clear
    resolveFetch(view()); // the in-flight fetch now resolves
    await vi.advanceTimersByTimeAsync(0); // run the onMount continuation
    await vi.advanceTimersByTimeAsync(3 * 2000 + 100); // fire any leaked interval

    // Only the mount fetch happened; no leaked interval kept polling.
    expect(api.getAdminDaemon).toHaveBeenCalledTimes(1);
  });
});

function container_hasNote(getByTestId: (id: string) => HTMLElement): boolean {
  try {
    getByTestId("daemon-external-note");
    return true;
  } catch {
    return false;
  }
}
