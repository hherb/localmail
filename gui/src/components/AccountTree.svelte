<script lang="ts">
  /**
   * Left rail. Renders "All Mail" + accounts (clickable, expandable to folders).
   *
   * Disclosure state (which accounts are expanded) lives in a local $state
   * Set; selection lives in the `mail` store so changes drive the message list.
   */
  import { mail } from "../lib/stores/mail.svelte";
  import { search } from "../lib/stores/search.svelte";

  let expanded: Set<string> = $state(new Set());
  // Plain (non-reactive) Set: tracks accounts whose first-ever folder load is
  // in flight. A rapid second click while loading would otherwise read the
  // already-flipped `expanded` and collapse the tree before folders arrive.
  const expansionsInFlight = new Set<string>();

  async function selectAll(): Promise<void> {
    mail.setSelection({ kind: "all" });
    // "All Mail" is the "go home" affordance — no scope means nothing to
    // search for. reset() clears tookMs so MessageList falls back to
    // mail.messages (recent-by-date), avoiding the empty-query / vector-arm
    // result pool (~20 arbitrary hits) that submit() would produce.
    search.reset();
  }

  async function selectAccount(accountId: string): Promise<void> {
    mail.setSelection({ kind: "account", accountId });
    if (expansionsInFlight.has(accountId)) return;
    if (expanded.has(accountId)) {
      const next = new Set(expanded);
      next.delete(accountId);
      expanded = next;
      return;
    }
    const next = new Set(expanded);
    next.add(accountId);
    expanded = next;
    expansionsInFlight.add(accountId);
    try {
      await mail.loadFoldersFor(accountId);
    } finally {
      expansionsInFlight.delete(accountId);
    }
    search.setFilters({ ...search.snapshot.filters, accountIds: [accountId], folderIds: [] });
    await search.submit();
  }

  async function selectFolder(accountId: string, folderId: string): Promise<void> {
    mail.setSelection({ kind: "folder", accountId, folderId });
    search.setFilters({ ...search.snapshot.filters, accountIds: [accountId], folderIds: [folderId] });
    await search.submit();
  }

  function isSelected(accountId: string | null, folderId: string | null): boolean {
    const sel = mail.snapshot.selection;
    if (accountId === null && folderId === null) return sel.kind === "all";
    if (folderId === null) {
      return sel.kind === "account" && sel.accountId === accountId;
    }
    return (
      sel.kind === "folder" &&
      sel.accountId === accountId &&
      sel.folderId === folderId
    );
  }
</script>

<aside class="tree">
  <ul>
    <li>
      <button
        type="button"
        class="row root"
        class:active={isSelected(null, null)}
        onclick={selectAll}
      >
        📥 All Mail
      </button>
    </li>
    {#each mail.snapshot.accounts as account (account.id)}
      <li>
        <button
          type="button"
          class="row account"
          class:active={isSelected(account.id, null)}
          onclick={() => selectAccount(account.id)}
        >
          <span class="caret">{expanded.has(account.id) ? "▾" : "▸"}</span>
          <span class="icon">{account.capabilities.is_archive_only ? "📦" : "✉️"}</span>
          <span class="label">{account.name}</span>
        </button>
        {#if expanded.has(account.id)}
          {@const folders = mail.snapshot.folders.get(account.id) ?? []}
          <ul class="folders">
            {#each folders as folder (folder.id)}
              <li>
                <button
                  type="button"
                  class="row folder"
                  class:active={isSelected(account.id, folder.id)}
                  onclick={() => selectFolder(account.id, folder.id)}
                >
                  {folder.name}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </li>
    {/each}
  </ul>
</aside>

<style>
  .tree {
    height: 100%;
    overflow-y: auto;
    background: #fafafa;
    border-right: 1px solid #e5e5e5;
    padding: 8px 0;
    font-size: 13px;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .row {
    display: block;
    width: 100%;
    text-align: left;
    padding: 6px 12px;
    background: none;
    border: none;
    color: #222;
    font: inherit;
    cursor: pointer;
  }
  .row:hover {
    background: #eef0f3;
  }
  .row.active {
    background: #d8e6ff;
    color: #1a4fc7;
    font-weight: 600;
  }
  .row.root {
    font-weight: 600;
  }
  .folders {
    padding-left: 14px;
  }
  .folder {
    padding-left: 24px;
    font-size: 12px;
  }
</style>
