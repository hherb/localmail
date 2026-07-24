/**
 * Pure derivations over a daemon view's heartbeat rows (project convention:
 * logic out of components, unit-tested in isolation).
 *
 * Structurally typed so this module stays independent of the Tauri API layer —
 * it only ever reads `account_id`.
 */

/** The subset of a heartbeat this module needs. */
interface HasAccountId {
  account_id: string | null;
}

/**
 * The account ids that should carry a "Restart sync" button, in first-seen
 * order and without duplicates.
 *
 * The daemon runs two workers (idle + poll) per account, so an account appears
 * in two heartbeat rows; the button acts on the account as a whole, so it is
 * rendered once. Heartbeats with no account id (none exist today, but the
 * column is nullable) are skipped. Mirrors the web panel's `seen_accounts`
 * dedup in `daemon/_status.html`.
 */
export function restartSyncAccountIds(heartbeats: readonly HasAccountId[]): string[] {
  const seen = new Set<string>();
  const ids: string[] = [];
  for (const hb of heartbeats) {
    const id = hb.account_id;
    if (id !== null && !seen.has(id)) {
      seen.add(id);
      ids.push(id);
    }
  }
  return ids;
}
