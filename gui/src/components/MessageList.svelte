<script lang="ts">
  /**
   * Middle pane. Filters the loaded message list by current selection,
   * renders one MessageListRow per visible message, dispatches clicks to
   * the mail store.
   */
  import MessageListRow from "./MessageListRow.svelte";
  import { selectionMatches } from "../lib/format";
  import { mail } from "../lib/stores/mail.svelte";

  function visibleMessages() {
    return mail.snapshot.messages.filter((m) =>
      selectionMatches(mail.snapshot.selection, m),
    );
  }

  async function openMessage(id: string): Promise<void> {
    await mail.openMessage(id);
  }
</script>

<section class="list">
  {#if mail.snapshot.loadingMessages}
    <div class="hint">Loading…</div>
  {:else}
    {@const items = visibleMessages()}
    {#if items.length === 0}
      <div class="hint">No messages.</div>
    {:else}
      {#each items as msg (msg.message_id)}
        <MessageListRow
          message={msg}
          selected={mail.snapshot.selectedMessage?.id === msg.message_id}
          onClick={() => openMessage(msg.message_id)}
        />
      {/each}
    {/if}
  {/if}
  {#if mail.snapshot.errorMessage}
    <div class="error">{mail.snapshot.errorMessage}</div>
  {/if}
</section>

<style>
  .list {
    height: 100%;
    overflow-y: auto;
    background: #fff;
    border-right: 1px solid #e5e5e5;
  }
  .hint {
    padding: 24px;
    text-align: center;
    color: #888;
    font-size: 13px;
  }
  .error {
    margin: 12px;
    padding: 8px 12px;
    background: #fdecec;
    border: 1px solid #f5c6c6;
    border-radius: 4px;
    color: #a02020;
    font-size: 12px;
    font-family: ui-monospace, SFMono-Regular, monospace;
  }
</style>
