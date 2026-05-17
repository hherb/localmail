<script lang="ts">
  /**
   * Single row in the message list.
   *
   * Props: `message` (the loaded summary), `selected` (highlight), `onClick`.
   * Kept tiny — the list owns the data, the row just renders one item.
   */
  import { addressLabel, formatRelativeDate, truncate } from "../lib/format";
  import type { MessageSummary } from "../lib/tauri";

  let {
    message,
    selected,
    onClick,
  }: {
    message: MessageSummary;
    selected: boolean;
    onClick: () => void;
  } = $props();

  const SUBJECT_TRUNCATE_CHARS = 64;
</script>

<button type="button" class="row" class:selected onclick={onClick}>
  <div class="top">
    <span class="from">{addressLabel(message.from)}</span>
    <span class="date">{formatRelativeDate(message.date)}</span>
  </div>
  <div class="subject">{truncate(message.subject, SUBJECT_TRUNCATE_CHARS) || "(no subject)"}</div>
  <div class="meta">
    {message.account.name ?? message.account.id}
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
  .meta {
    margin-top: 2px;
    font-size: 11px;
    color: #888;
  }
</style>
