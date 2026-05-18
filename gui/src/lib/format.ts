/**
 * Pure helpers shared by the list / tree / reading-pane components.
 *
 * No DOM, no Tauri invokes, no $state. Test as pure functions only.
 */

import type { MessageAddress, MessageSummary, Selection } from "./tauri";

const FALLBACK_SENDER = "(unknown sender)";
const ELLIPSIS = "…";

/**
 * Display label for a sender / recipient: `name` if present, else `address`,
 * else a placeholder. The MessageAddress shape allows both fields nullable —
 * real mail occasionally has neither.
 */
export function addressLabel(addr: MessageAddress): string {
  if (addr.name && addr.name.trim()) return addr.name.trim();
  if (addr.address && addr.address.trim()) return addr.address.trim();
  return FALLBACK_SENDER;
}

/**
 * Truncate `s` to `maxChars` characters, appending an ellipsis if the input
 * was longer. `null`/`undefined` → empty string. The trimmed character count
 * does NOT include the appended ellipsis.
 */
export function truncate(s: string | null | undefined, maxChars: number): string {
  if (s == null) return "";
  if (s.length <= maxChars) return s;
  return s.slice(0, maxChars) + ELLIPSIS;
}

/**
 * Format a message date relative to `now`:
 *   - same calendar day  → time only (e.g. "9:30 AM" / "09:30")
 *   - same calendar year → short date (e.g. "Mar 3")
 *   - older              → year-qualified date (e.g. "Dec 25, 2024")
 * Returns "" for null / unparseable input.
 *
 * Format is locale-driven via Intl; we don't pin a locale because the user's
 * OS locale is the right default for a desktop client.
 */
export function formatRelativeDate(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "";
  if (sameDay(dt, now)) {
    return dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }
  if (dt.getFullYear() === now.getFullYear()) {
    return dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  return dt.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/**
 * True if `msg` should be visible under the current selection.
 *
 * `folder` selection narrows by account only (folder narrowing is server-side
 * and deferred to Sub-plan 4 — the loaded `/v1/changes` response does not
 * tell us which folder each message belongs to). Treating "folder"
 * functionally like "account" keeps the UI honest while we wait.
 */
export function selectionMatches(sel: Selection, msg: MessageSummary): boolean {
  switch (sel.kind) {
    case "all":
      return true;
    case "account":
      return msg.account.id === sel.accountId;
    case "folder":
      return msg.account.id === sel.accountId;
  }
}
