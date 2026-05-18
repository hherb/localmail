/**
 * Thin wrapper over the `get_message_raw_cmd` Tauri command.
 *
 * Tauri serialises `Vec<u8>` as a JSON `number[]` over IPC; we re-wrap it as
 * a `Uint8Array` so callers can hand it straight to a Blob / decoder.
 */
import { invoke } from "@tauri-apps/api/core";

export async function getRawMessage(id: string): Promise<Uint8Array> {
  // Tauri converts top-level camelCase keys to snake_case Rust parameters.
  // Matches the {messageId}-shape used by full_headers / get_message_cmd —
  // keep the convention uniform across commands to avoid bug-prone drift.
  const bytes = await invoke<number[]>("get_message_raw_cmd", {
    messageId: id,
  });
  return new Uint8Array(bytes);
}
