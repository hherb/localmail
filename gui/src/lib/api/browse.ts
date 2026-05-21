/**
 * Wire types + Tauri wrapper for GET /v1/messages.
 *
 * Mirrors the Rust ListMessagesRequest/Response in
 * src-tauri/src/commands/browse.rs.
 */
import { invoke } from "@tauri-apps/api/core";

import type { MessageSummary } from "./types";

export interface ListMessagesRequest {
  account_ids: string[];
  folder_ids: string[];
  limit: number;
  cursor: string | null;
}

export interface ListMessagesResponse {
  messages: MessageSummary[];
  next_cursor: string | null;
}

export async function listMessages(
  req: ListMessagesRequest,
): Promise<ListMessagesResponse> {
  return invoke<ListMessagesResponse>("list_messages_cmd", { req });
}
