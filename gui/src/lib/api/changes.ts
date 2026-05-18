import { invoke } from "@tauri-apps/api/core";

import type { ChangesResponse } from "./types";

export type { MessageSummary, ChangesResponse } from "./types";

/**
 * Fetch the latest changes from the server.
 *
 * `cursor` is accepted for forward compatibility with the polling loop. The
 * current `list_recent_messages_cmd` Rust binding ignores extra args, so
 * passing a cursor today is a no-op on the wire; the Rust side will start
 * forwarding it once the `?since=` query param is wired through. Until then,
 * each poll re-fetches the most-recent window and the store deduplicates.
 */
export async function getChanges(cursor: string | null = null): Promise<ChangesResponse> {
  return invoke<ChangesResponse>("list_recent_messages_cmd", { since: cursor });
}
