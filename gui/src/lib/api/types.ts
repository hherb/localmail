/**
 * Shared API response types. Mirrors the Rust structs in
 * src-tauri/src/commands/{accounts,changes,messages}.rs, which themselves
 * mirror the JSON returned by the server.
 *
 * Keep this file dependency-free — pure type declarations. Stores and
 * components import from here; the invoke wrappers live in tauri.ts.
 */

export interface AccountCapabilities {
  can_sync: boolean;
  is_archive_only: boolean;
  is_shared: boolean;
}

export interface AccountSummary {
  id: string;
  name: string;
  address: string | null;
  last_sync_at: string | null;
  message_count: number;
  capabilities: AccountCapabilities;
}

export interface FolderSummary {
  id: string;
  name: string;
  full_path: string;
  flags: string | null;
  last_uid: number | null;
  message_count: number;
}

export interface MessageAddress {
  address: string | null;
  name: string | null;
}

export interface MessageAccount {
  id: string;
  name: string | null;
}

export interface MessageSummary {
  message_id: string;
  subject: string | null;
  from: MessageAddress;
  date: string | null;
  account: MessageAccount;
}

export interface ChangesResponse {
  new_messages: MessageSummary[];
  next_cursor: string | null;
}

export interface MessageFolder {
  id: string;
  name: string;
}

export interface MessageDetailAccount {
  id: string;
  name: string | null;
  address: string | null;
}

export interface MessageAttachment {
  filename: string | null;
  sha256: string | null;
}

export interface MatchedChunk {
  kind: string;
  text: string;
  score?: number;
}

export interface MessageDetail {
  id: string;
  subject: string | null;
  from: MessageAddress;
  to: MessageAddress[];
  cc: MessageAddress[];
  bcc: MessageAddress[];
  date: string | null;
  body_text: string | null;
  body_html: string | null;
  attachments: MessageAttachment[];
  account: MessageDetailAccount;
  folders: MessageFolder[];
  // Populated only when the caller requested ?headers=full (see
  // src/lib/api/full_headers.ts). Flat map of raw header name → value;
  // headers that occurred multiple times come back as a string[].
  headers?: Record<string, string | string[]> | null;
  // Populated when the message was opened via a search result that returned
  // chunk-level matches. Used by the debug-mode DebugChunks pane in
  // ReadingPane. Server wiring is best-effort and may leave this undefined.
  matched_chunks?: MatchedChunk[];
}

/**
 * What the user has selected in the left rail. Drives which subset of the
 * loaded message list the middle pane shows.
 *
 * - `all`     — "All Mail" pinned entry. Shows everything in the loaded set.
 * - `account` — narrow to one account (filters loaded messages by account.id).
 * - `folder`  — narrow to one folder of one account. Folder filtering is
 *               client-side until Sub-plan 4 wires server-side folder_ids.
 */
export type Selection =
  | { kind: "all" }
  | { kind: "account"; accountId: string }
  | { kind: "folder"; accountId: string; folderId: string };
