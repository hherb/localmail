<script lang="ts">
  import MessageListRow from "./MessageListRow.svelte";
  import { mail } from "../lib/stores/mail.svelte";
  import { search } from "../lib/stores/search.svelte";

  let searchActive = $derived(search.snapshot.tookMs !== null);

  interface ListRow {
    id: string;
    subject: string | null;
    from: { address: string | null; name: string | null };
    date: string | null;
    account: { id: string; name: string | null };
    snippet: string | null;
  }

  let rows: ListRow[] = $derived(
    searchActive
      ? search.snapshot.results.map((r) => ({
          id: r.message_id,
          subject: r.subject,
          from: r.from,
          date: r.date,
          account: r.account,
          snippet: r.snippet_html,
        }))
      : mail.snapshot.messages.map((m) => ({
          id: m.message_id,
          subject: m.subject,
          from: m.from,
          date: m.date,
          account: m.account,
          snippet: null,
        })),
  );

  let visible = $derived(
    searchActive
      ? rows
      : rows.filter((r) => {
          const sel = mail.snapshot.selection;
          if (sel.kind === "all") return true;
          return r.account.id === sel.accountId;
        }),
  );

  async function openMessage(id: string): Promise<void> {
    await mail.openMessage(id);
  }
</script>

<section class="list">
  {#if searchActive}
    <div class="caption">
      Search took {Math.round(search.snapshot.tookMs ?? 0)} ms — {search.snapshot.results.length} result(s)
    </div>
  {/if}
  {#if search.snapshot.errorMessage}
    <div class="error">{search.snapshot.errorMessage}</div>
  {:else if mail.snapshot.errorMessage}
    <div class="error">{mail.snapshot.errorMessage}</div>
  {/if}
  {#if mail.snapshot.loadingMessages && !searchActive}
    <div class="hint">Loading…</div>
  {:else if visible.length === 0}
    {#if searchActive}
      <div class="hint">No matches.</div>
    {:else}
      <div class="hint">No messages.</div>
    {/if}
  {:else}
    {#each visible as r (r.id)}
      <MessageListRow
        subject={r.subject}
        from={r.from}
        date={r.date}
        account={r.account}
        snippet={r.snippet}
        selected={mail.snapshot.selectedMessage?.id === r.id}
        onSelect={() => openMessage(r.id)}
      />
    {/each}
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
  .caption {
    padding: 4px 12px;
    font-size: 11px;
    color: #666;
    background: #fafbfd;
    border-bottom: 1px solid #eee;
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
