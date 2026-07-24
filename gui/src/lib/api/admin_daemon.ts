/**
 * Typed wrappers over the admin-daemon Tauri commands, which proxy
 * `/v1/admin/daemon*` and `/v1/admin/accounts/{id}/restart-sync` with the
 * stored bearer token.
 *
 * Two planes, mirroring the server:
 *   - lifecycle (start/stop/restart) drives the in-process supervisor; under an
 *     externally-supervised daemon these return 409 and the UI disables them.
 *   - reload / restart-sync enqueue DB commands and work regardless of who
 *     owns the daemon process.
 */
import { invoke } from "@tauri-apps/api/core";

export interface DaemonHeartbeat {
  worker_kind: string;
  account_id: string | null;
  state: string;
  current_folder: string | null;
  last_error_msg: string | null;
  started_at: string;
  last_heartbeat_at: string;
  stale: boolean;
}

export interface DaemonView {
  state: string;
  pid: number | null;
  started_at: string | null;
  supervise_daemon_externally: boolean;
  heartbeats: DaemonHeartbeat[];
  recent_log: string[];
}

export interface DaemonStatus {
  state: string;
  pid: number | null;
  started_at: string | null;
}

export interface CommandAck {
  command_id: string;
}

export type LifecycleOp = "start" | "stop" | "restart";

export async function getAdminDaemon(): Promise<DaemonView> {
  return invoke<DaemonView>("get_admin_daemon_cmd");
}

export async function lifecycleAdminDaemon(op: LifecycleOp): Promise<DaemonStatus> {
  return invoke<DaemonStatus>("lifecycle_admin_daemon_cmd", { op });
}

export async function startAdminDaemon(): Promise<DaemonStatus> {
  return lifecycleAdminDaemon("start");
}

export async function stopAdminDaemon(): Promise<DaemonStatus> {
  return lifecycleAdminDaemon("stop");
}

export async function restartAdminDaemon(): Promise<DaemonStatus> {
  return lifecycleAdminDaemon("restart");
}

export async function reloadAdminDaemon(): Promise<CommandAck> {
  return invoke<CommandAck>("reload_admin_daemon_cmd");
}

export async function restartAccountSync(accountId: string): Promise<CommandAck> {
  return invoke<CommandAck>("restart_account_sync_cmd", { accountId });
}
