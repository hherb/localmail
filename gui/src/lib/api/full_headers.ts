import { invoke } from "@tauri-apps/api/core";

import type { MessageDetail } from "./messages";

export type { MessageDetail };

/**
 * Fetch a message including the full raw-headers map.
 *
 * Calls `GET /v1/messages/{id}?headers=full` via the Rust
 * `get_message_full_headers_cmd` Tauri command. The returned `headers`
 * field is a flat object of raw header name → value (string or string[]
 * when a header appeared multiple times, e.g. `Received`).
 */
export async function getMessageFullHeaders(id: string): Promise<MessageDetail> {
  return invoke<MessageDetail>("get_message_full_headers_cmd", { messageId: id });
}
