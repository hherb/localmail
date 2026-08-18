<script lang="ts">
  import { addressLabel, formatMessageDate, truncate } from "../lib/format";
  import { sanitizeSnippet } from "../lib/snippet_sanitize";
  import { settings } from "../lib/stores/settings.svelte";
  import type { MessageAddress, MessageAccount } from "../lib/tauri";

  let {
    subject,
    from,
    date,
    account,
    snippet = null,
    selected,
    onSelect,
  }: {
    subject: string | null;
    from: MessageAddress;
    date: string | null;
    account: MessageAccount;
    snippet?: string | null;
    selected: boolean;
    onSelect: () => void;
  } = $props();

  const SUBJECT_TRUNCATE_CHARS = 64;
</script>

<button
  type="button"
  class="row"
  class:selected
  class:compact={settings.snapshot.density === "compact"}
  onclick={onSelect}
>
  <div class="top">
    <span class="from">{addressLabel(from)}</span>
    <span class="date">{formatMessageDate(date, settings.snapshot.dateFormat)}</span>
  </div>
  <div class="subject">{truncate(subject, SUBJECT_TRUNCATE_CHARS) || "(no subject)"}</div>
  {#if snippet}
    <div class="snippet">{@html sanitizeSnippet(snippet)}</div>
  {/if}
  <div class="meta">
    {account.name ?? account.id}
  </div>
</button>

<style>
  .row {
    display: block;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    border: none;
    border-bottom: 1px solid var(--border);
    border-radius: 0;
    padding: 11px 13px;
    font: inherit;
    cursor: pointer;
  }
  .row:hover {
    background: var(--surface-subtle);
  }
  .row.selected {
    background: var(--accent-soft);
    box-shadow: inset 3px 0 0 var(--accent);
  }
  .row.compact {
    padding-top: 6px;
    padding-bottom: 6px;
  }
  .row.compact .snippet {
    display: none;
  }
  .row.compact .meta {
    margin-top: 0;
  }
  .top {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 12px;
    color: var(--fg-muted);
  }
  .from {
    font-weight: 600;
    color: var(--fg);
  }
  .date {
    flex-shrink: 0;
    margin-left: 8px;
    color: var(--fg-faint);
    font-size: 10px;
  }
  .subject {
    margin-top: 2px;
    font-size: 13px;
    color: var(--fg);
  }
  .snippet {
    font-size: 11px;
    color: var(--fg-muted);
    line-height: 1.3;
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .snippet :global(mark) {
    background: #fff0ae;
    padding: 0 1px;
    border-radius: 2px;
  }
  .meta {
    margin-top: 2px;
    font-size: 11px;
    color: var(--fg-faint);
  }
</style>
