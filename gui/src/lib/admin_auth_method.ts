/**
 * Pure predicates over an account's auth method.
 *
 * Both admin account components ask the same two questions, and asking
 * them through a function also keeps TypeScript from narrowing a local
 * `$state` variable to its initialiser's literal type — at which point a
 * comparison against another member of the union looks unreachable.
 */
import type { AdminAuthMethod } from "./api/admin_accounts";

/** Archive accounts are pure containers for imported mail — no IMAP server. */
export function hasImapEndpoint(method: AdminAuthMethod): boolean {
  return method !== "archive";
}

/** oauth2 stores a refresh token from the consent flow, not a password. */
export function usesStoredPassword(method: AdminAuthMethod): boolean {
  return method === "password";
}
