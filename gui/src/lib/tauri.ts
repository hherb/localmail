/**
 * Thin typed wrappers around Tauri's invoke().
 *
 * Each exported function corresponds to one #[tauri::command] in src-tauri/.
 */
import { invoke } from "@tauri-apps/api/core";
import type {
  AccountSummary,
  ChangesResponse,
  FolderSummary,
  MessageDetail,
} from "./api/types";
import type { SearchRequest, SearchResponse } from "./api/search";

export type {
  AccountCapabilities,
  AccountSummary,
  ChangesResponse,
  FolderSummary,
  MessageAccount,
  MessageAddress,
  MessageAttachment,
  MessageDetail,
  MessageDetailAccount,
  MessageFolder,
  MessageSummary,
  Selection,
} from "./api/types";
export type {
  SearchAccount,
  SearchAddress,
  SearchFiltersUI,
  SearchFiltersWire,
  SearchFolder,
  SearchRequest,
  SearchResponse,
  SearchResultRow,
} from "./api/search";
export { emptyFilters, filtersUiToWire } from "./api/search";

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

export async function listAccounts(): Promise<AccountSummary[]> {
  return invoke<AccountSummary[]>("list_accounts_cmd");
}

export async function listFolders(accountId: string): Promise<FolderSummary[]> {
  return invoke<FolderSummary[]>("list_folders_cmd", { accountId });
}

export async function listRecentMessages(): Promise<ChangesResponse> {
  return invoke<ChangesResponse>("list_recent_messages_cmd");
}

export async function getMessage(messageId: string): Promise<MessageDetail> {
  return invoke<MessageDetail>("get_message_cmd", { messageId });
}

export async function runSearch(req: SearchRequest): Promise<SearchResponse> {
  return invoke<SearchResponse>("run_search_cmd", { req });
}

export interface DownloadResult {
  bytes_written: number;
  path: string;
}

export async function downloadAttachment(sha256: string, dest: string): Promise<DownloadResult> {
  return invoke<DownloadResult>("download_attachment_cmd", { sha256, dest });
}

export interface AttachmentBlob {
  bytes: number[];
  content_type: string | null;
}

export async function fetchAttachmentBytes(sha256: string): Promise<AttachmentBlob> {
  return invoke<AttachmentBlob>("fetch_attachment_bytes_cmd", { sha256 });
}
