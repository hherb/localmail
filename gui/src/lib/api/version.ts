/**
 * TS wrapper for the `get_version_cmd` Tauri command. Mirrors the
 * `VersionInfo` Rust struct in src-tauri/src/commands/version.rs. Used
 * by the version store at startup to gate the UI on protocol compatibility.
 */
import { invoke } from "@tauri-apps/api/core";
import type { VersionInfo as VersionShape } from "../version_check";

export interface ServerVersionInfo extends VersionShape {
  server_version: string | null;
  build_hash: string | null;
  // Optional, not `string | null`: the *server* always sends these, but this
  // client can be talking to one that predates them. That is the same reason
  // `buildLabel`/`versionWarning` accept `null | undefined` — and it is why the
  // mock sites that predate this change (MainView, VersionGate, version.test,
  // SettingsAbout) need no update. Phrased as an invariant, not a count: a
  // count rots on the next version test anyone adds.
  build_source?: string | null;
  version_source?: string | null;
}

export async function getVersion(): Promise<ServerVersionInfo> {
  return invoke<ServerVersionInfo>("get_version_cmd");
}
