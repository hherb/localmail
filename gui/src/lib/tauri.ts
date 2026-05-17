/**
 * Thin typed wrappers around Tauri's invoke().
 *
 * Each exported function corresponds to one #[tauri::command] in src-tauri/.
 */
import { invoke } from "@tauri-apps/api/core";

export interface Greeting {
  message: string;
  source: string;
}

export interface ProbeResult {
  api_major: number;
  api_minor: number;
  server_version: string;
  cert_sha256: string;
}

export interface LoginSummary {
  username: string;
  expires_at: string;
}

export interface WhoamiResponse {
  username: string;
  user_id: string;
}

export interface Capabilities {
  search: boolean;
  attachments: boolean;
  attachment_text: boolean;
  threading: boolean;
  send: boolean;
}

export async function greet(name: string): Promise<Greeting> {
  return invoke<Greeting>("greet", { name });
}

export async function probeServer(url: string): Promise<ProbeResult> {
  return invoke<ProbeResult>("probe_server_cmd", { url });
}

export async function confirmTrust(url: string, certSha256: string): Promise<void> {
  return invoke<void>("confirm_trust_cmd", { url, certSha256 });
}

export async function login(username: string, password: string): Promise<LoginSummary> {
  return invoke<LoginSummary>("login_cmd", { username, password });
}

export async function logout(): Promise<void> {
  return invoke<void>("logout_cmd");
}

export async function refresh(): Promise<LoginSummary> {
  return invoke<LoginSummary>("refresh_cmd");
}

export async function whoami(): Promise<WhoamiResponse> {
  return invoke<WhoamiResponse>("whoami_cmd");
}

export async function getCapabilities(): Promise<Capabilities> {
  return invoke<Capabilities>("get_capabilities_cmd");
}
