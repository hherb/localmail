/**
 * TS wrapper for the `change_password_cmd` Tauri command. Invokes
 * POST /v1/auth/change-password on the server with the current bearer
 * token; on 204 the existing token stays valid and the next login will
 * accept the new password.
 */
import { invoke } from "@tauri-apps/api/core";

export async function changePassword(
  oldPassword: string,
  newPassword: string,
): Promise<void> {
  await invoke<void>("change_password_cmd", { oldPassword, newPassword });
}
