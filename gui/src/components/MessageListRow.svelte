<script lang="ts">
  import { addressLabel, formatRelativeDate, truncate } from "../lib/format";
  import { sanitizeSnippet } from "../lib/snippet_sanitize";
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

<button type="button" class="row" class:selected onclick={onSelect}>
  <div class="top">
    <span class="from">{addressLabel(from)}</span>
    <span class="date">{formatRelativeDate(date)}</span>
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
    border-bottom: 1px solid #ececec;
    padding: 8px 12px;
    font: inherit;
    cursor: pointer;
  }
  .row:hover {
    background: #f4f6f9;
  }
  .row.selected {
    background: #d8e6ff;
  }
  .top {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 12px;
    color: #555;
  }
  .from {
    font-weight: 600;
    color: #222;
  }
  .date {
    flex-shrink: 0;
    margin-left: 8px;
  }
  .subject {
    margin-top: 2px;
    font-size: 13px;
    color: #222;
  }
  .snippet {
    font-size: 11px;
    color: #555;
    line-height: 1.3;
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .snippet :global(mark) {
    background: #ffe89d;
    padding: 0 1px;
    border-radius: 2px;
  }
  .meta {
    margin-top: 2px;
    font-size: 11px;
    color: #888;
  }
</style>
