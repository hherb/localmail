import type { MessageSummary } from "./api/changes";

export const POLL_INTERVAL_MS = 30_000;

export function dedupNewMessages(
  existing: readonly MessageSummary[],
  incoming: readonly MessageSummary[],
): MessageSummary[] {
  const seen = new Set(existing.map((m) => m.message_id));
  return incoming.filter((m) => !seen.has(m.message_id));
}

export function parseCursor(raw: string | null): string | null {
  if (raw === null) return null;
  if (raw.trim() === "") return null;
  return raw;
}
