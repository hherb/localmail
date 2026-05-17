/**
 * Thin typed wrappers around Tauri's invoke().
 *
 * Each exported function corresponds to one #[tauri::command] in src-tauri/src/lib.rs.
 * Adding a command means: declare it in Rust, then add a wrapper here.
 */
import { invoke } from "@tauri-apps/api/core";

export interface Greeting {
  message: string;
  source: string;
}

export async function greet(name: string): Promise<Greeting> {
  return invoke<Greeting>("greet", { name });
}
