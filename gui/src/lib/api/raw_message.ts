/**
 * Thin wrapper over the `get_message_raw_cmd` Tauri command.
 *
 * Tauri serialises `Vec<u8>` as a JSON `number[]` over IPC; we re-wrap it as
 * a `Uint8Array` so callers can hand it straight to a Blob / decoder.
 */
import { invoke } from "@tauri-apps/api/core";

export async function getRawMessage(id: string): Promise<Uint8Array> {
  const bytes = await invoke<number[]>("get_message_raw_cmd", {
    args: { message_id: id },
  });
  return new Uint8Array(bytes);
}
