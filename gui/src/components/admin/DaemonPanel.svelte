<script lang="ts">
  /**
   * Admin → Daemon. Fuses the supervisor's process state, per-worker
   * heartbeats, and recent log lines from `GET /v1/admin/daemon`, and offers
   * the two control planes the server exposes:
   *   - lifecycle (start / stop / restart) — disabled when the daemon is
   *     supervised externally (launchd / systemd owns the process), matching
   *     the web admin panel exactly.
   *   - DB-mediated controls (reload, per-account restart-sync) — always
   *     available; they enqueue commands the running daemon consumes.
   *
   * The view self-refreshes on a fixed interval. Staleness is the server's
   * `stale` flag alone (computed against `heartbeat_stale_seconds`), never a
   * client clock. A rejected control (busy-guard / external 409, or any other
   * error) surfaces as a visible message rather than an inert button (#148).
   */
  import { onDestroy, onMount } from "svelte";

  import {
    getAdminDaemon,
    reloadAdminDaemon,
    restartAccountSync,
    restartAdminDaemon,
    startAdminDaemon,
    stopAdminDaemon,
    type DaemonView,
    type LifecycleOp,
  } from "../../lib/api/admin_daemon";
  import { isConflict } from "../../lib/admin_error";
  import { restartSyncAccountIds } from "../../lib/daemon_view";
  import { formatError } from "../../lib/format_error";

  // Mirrors the web panel's DAEMON_PANEL_POLL_SECONDS = 2.
  const POLL_INTERVAL_MS = 2000;
  const ACCEPTED_MESSAGE = "Request accepted.";
  const BUSY_MESSAGE =
    "Another daemon operation is already in progress. Try again in a moment.";

  let view: DaemonView | null = $state(null);
  let loading: boolean = $state(true);
  let errorMessage: string | null = $state(null);
  let busy: boolean = $state(false);
  let actionMessage: string | null = $state(null);
  let actionKind: "ok" | "error" = $state("ok");

  let pollTimer: ReturnType<typeof setInterval> | null = null;
  // The initial fetch is awaited before the poll interval is started, so an
  // unmount *during* that fetch would otherwise run onDestroy (pollTimer still
  // null) and then start an interval onto the dead component. This flag makes
  // the post-await start a no-op in that race.
  let destroyed = false;

  onMount(async () => {
    await fetchView({ showSpinner: true });
    if (destroyed) return;
    // Silent background refresh; a transient failure updates the error line
    // but never wipes the last good view.
    pollTimer = setInterval(() => {
      void fetchView({ showSpinner: false });
    }, POLL_INTERVAL_MS);
  });

  onDestroy(() => {
    destroyed = true;
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  });

  async function fetchView(opts: { showSpinner: boolean }): Promise<void> {
    if (opts.showSpinner) loading = true;
    try {
      const result = await getAdminDaemon();
      // An unwired bridge resolves undefined instead of rejecting; treat any
      // non-object (or one missing the heartbeats array) as a failure so it
      // reaches the error state rather than crashing the template.
      if (!result || typeof result !== "object" || !Array.isArray(result.heartbeats)) {
        throw new Error("unexpected response from get_admin_daemon_cmd");
      }
      view = result;
      errorMessage = null;
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      if (opts.showSpinner) loading = false;
    }
  }

  function report(err: unknown): void {
    actionKind = "error";
    actionMessage = isConflict(err) ? BUSY_MESSAGE : formatError(err);
  }

  async function runAction(action: () => Promise<unknown>): Promise<void> {
    busy = true;
    actionMessage = null;
    try {
      await action();
      actionKind = "ok";
      actionMessage = ACCEPTED_MESSAGE;
      await fetchView({ showSpinner: false });
    } catch (err: unknown) {
      report(err);
    } finally {
      busy = false;
    }
  }

  const LIFECYCLE: Record<LifecycleOp, () => Promise<unknown>> = {
    start: startAdminDaemon,
    stop: stopAdminDaemon,
    restart: restartAdminDaemon,
  };

  function onLifecycle(op: LifecycleOp): Promise<void> {
    return runAction(LIFECYCLE[op]);
  }

  function onReload(): Promise<void> {
    return runAction(reloadAdminDaemon);
  }

  function onRestartSync(accountId: string): Promise<void> {
    return runAction(() => restartAccountSync(accountId));
  }
</script>

<div class="panel">
  <div class="toolbar">
    <button
      data-testid="daemon-refresh"
      onclick={() => fetchView({ showSpinner: false })}
      disabled={busy}
    >Refresh</button>
  </div>

  {#if errorMessage}
    <p class="error" data-testid="daemon-error" role="alert">{errorMessage}</p>
  {/if}

  {#if actionMessage}
    <p
      class="action-message"
      class:error={actionKind === "error"}
      data-testid="daemon-action-message"
      role="status"
    >{actionMessage}</p>
  {/if}

  {#if loading}
    <p data-testid="daemon-loading">Loading daemon status…</p>
  {:else if view}
    <!-- One restart-sync action per account (idle + poll workers collapse to
         one). Computed inside this block, where `view` is narrowed non-null. -->
    {@const restartAccounts = restartSyncAccountIds(view.heartbeats)}
    <section class="process">
      <h3>Process</h3>
      <p data-testid="daemon-state">
        State: <strong>{view.state}</strong>
        {#if view.pid}(pid {view.pid}){/if}
        {#if view.started_at}— started {view.started_at}{/if}
      </p>
      {#if view.supervise_daemon_externally}
        <p class="note" data-testid="daemon-external-note">
          Daemon is supervised externally; start / stop / restart are managed by
          your init system, not here.
        </p>
      {/if}
      <div class="controls">
        <button
          data-testid="daemon-start"
          onclick={() => onLifecycle("start")}
          disabled={busy || view.supervise_daemon_externally}
        >Start</button>
        <button
          data-testid="daemon-stop"
          onclick={() => onLifecycle("stop")}
          disabled={busy || view.supervise_daemon_externally}
        >Stop</button>
        <button
          data-testid="daemon-restart"
          onclick={() => onLifecycle("restart")}
          disabled={busy || view.supervise_daemon_externally}
        >Restart</button>
        <button
          data-testid="daemon-reload"
          onclick={onReload}
          disabled={busy}
        >Reload now</button>
      </div>
    </section>

    <section class="workers">
      <h3>Workers</h3>
      {#if view.heartbeats.length === 0}
        <p data-testid="daemon-heartbeats-empty">No heartbeats recorded.</p>
      {:else}
        <table>
          <thead>
            <tr>
              <th>Worker</th><th>Account</th><th>State</th><th>Folder</th>
              <th>Last heartbeat</th><th>Error</th>
            </tr>
          </thead>
          <tbody>
            {#each view.heartbeats as hb, i (i)}
              <tr class:daemon-stale={hb.stale} data-testid="heartbeat-row-{i}">
                <td>{hb.worker_kind}</td>
                <td>{hb.account_id ?? "—"}</td>
                <td>{hb.state}</td>
                <td>{hb.current_folder ?? "—"}</td>
                <td>
                  {hb.last_heartbeat_at}
                  {#if hb.stale}<span class="stale-tag">stale</span>{/if}
                </td>
                <td>{hb.last_error_msg ?? ""}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}

      {#if restartAccounts.length > 0}
        <div class="account-controls">
          <span class="account-controls-label">Restart sync for account:</span>
          {#each restartAccounts as accountId (accountId)}
            <button
              data-testid="restart-sync-{accountId}"
              onclick={() => onRestartSync(accountId)}
              disabled={busy}
            >#{accountId}</button>
          {/each}
        </div>
      {/if}
    </section>

    <section class="log">
      <h3>Recent log</h3>
      {#if view.recent_log.length === 0}
        <p data-testid="daemon-log-empty">No log lines captured.</p>
      {:else}
        <pre data-testid="daemon-log">{view.recent_log.join("\n")}</pre>
      {/if}
    </section>
  {/if}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    font-size: 0.9rem;
  }
  .toolbar {
    display: flex;
    gap: 0.5rem;
  }
  section h3 {
    margin: 0.5rem 0 0.35rem;
    font-size: 0.95rem;
    color: #555;
  }
  .controls,
  .account-controls {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: center;
    margin-top: 0.4rem;
  }
  .account-controls-label {
    color: #555;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    padding: 0.35rem 0.5rem;
    border-bottom: 1px solid #eee;
    vertical-align: top;
  }
  th {
    font-weight: 600;
    color: #555;
  }
  tr.daemon-stale td {
    background: #fff4f3;
    color: #b3261e;
  }
  .stale-tag {
    margin-left: 0.35rem;
    font-size: 0.75rem;
    color: #b3261e;
    font-weight: 600;
  }
  pre {
    background: #f7f9fc;
    padding: 0.5rem;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0;
  }
  .note {
    color: #8a6d00;
    margin: 0.25rem 0;
  }
  .error {
    color: #b3261e;
    margin: 0;
  }
  .action-message {
    margin: 0;
    color: #1a7f37;
  }
  .action-message.error {
    color: #b3261e;
  }
</style>
