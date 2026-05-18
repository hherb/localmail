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
}

export async function getVersion(): Promise<ServerVersionInfo> {
  return invoke<ServerVersionInfo>("get_version_cmd");
}
