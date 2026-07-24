/**
 * Typed wrappers over the admin-account Tauri commands, which proxy
 * `/v1/admin/accounts*` with the stored bearer token.
 *
 * `AdminAccountPatch` fields are optional by design: the Rust layer omits
 * unset keys from the PATCH body, and the server writes every key it
 * receives — sending an explicit null would blank the column.
 */
import { invoke } from "@tauri-apps/api/core";

export type AdminAuthMethod = "password" | "oauth2" | "archive";

export interface AdminAccountSummary {
  id: string;
  name: string;
  email_address: string;
  auth_method: AdminAuthMethod;
  sync_enabled: boolean;
}

export interface AdminAccount {
  id: string;
  name: string;
  email_address: string;
  auth_method: AdminAuthMethod;
  oauth_provider: string | null;
  imap_host: string | null;
  imap_port: number | null;
  folder_allow: string[] | null;
  folder_deny: string[] | null;
  folder_deny_flags: string[] | null;
  sync_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminAccountInput {
  name: string;
  email_address: string;
  auth_method: AdminAuthMethod;
  imap_host?: string;
  imap_port?: number;
  oauth_provider?: string;
  folder_allow?: string[];
  folder_deny?: string[];
  folder_deny_flags?: string[];
}

export interface AdminAccountPatch {
  email_address?: string;
  auth_method?: AdminAuthMethod;
  imap_host?: string;
  imap_port?: number;
  oauth_provider?: string;
  folder_allow?: string[];
  folder_deny?: string[];
  folder_deny_flags?: string[];
  sync_enabled?: boolean;
}

export interface ProbedFolder {
  name: string;
  flags: string[];
}

export interface TestConnectionResult {
  folders: ProbedFolder[];
}

export async function listAdminAccounts(): Promise<AdminAccountSummary[]> {
  return invoke<AdminAccountSummary[]>("list_admin_accounts_cmd");
}

export async function getAdminAccount(accountId: string): Promise<AdminAccount> {
  return invoke<AdminAccount>("get_admin_account_cmd", { accountId });
}

export async function createAdminAccount(
  input: AdminAccountInput,
): Promise<AdminAccount> {
  return invoke<AdminAccount>("create_admin_account_cmd", { input });
}

export async function updateAdminAccount(
  accountId: string,
  patch: AdminAccountPatch,
): Promise<AdminAccount> {
  return invoke<AdminAccount>("update_admin_account_cmd", { accountId, patch });
}

export async function deleteAdminAccount(
  accountId: string,
  force: boolean = false,
): Promise<void> {
  return invoke<void>("delete_admin_account_cmd", { accountId, force });
}

export async function storeAdminAccountPassword(
  accountId: string,
  password: string,
): Promise<void> {
  return invoke<void>("store_admin_account_password_cmd", { accountId, password });
}

export async function testAdminAccountConnection(
  accountId: string,
): Promise<TestConnectionResult> {
  return invoke<TestConnectionResult>("test_admin_account_connection_cmd", {
    accountId,
  });
}
