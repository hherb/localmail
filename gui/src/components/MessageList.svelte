<script lang="ts">
  import { onMount, onDestroy } from "svelte";
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

  let hasMore = $derived(
    searchActive ? search.snapshot.hasMore : mail.messagesHasMore,
  );

  let loadingMore = $derived(
    searchActive ? search.snapshot.loadingMore : mail.snapshot.loadingMore,
  );

  let pendingCount = $derived(mail.snapshot.pendingNewMessages.length);

  async function loadMore(): Promise<void> {
    if (searchActive) {
      await search.loadMore();
    } else {
      await mail.loadMoreMessages();
    }
  }

  function mergePending(): void {
    mail.mergePendingNewMessages();
  }

  async function openMessage(id: string): Promise<void> {
    await mail.openMessage(id);
  }

  // IntersectionObserver-driven auto-load on near-bottom scroll.
  let sentinel: HTMLDivElement | undefined = $state();
  let observer: IntersectionObserver | undefined;

  onMount(() => {
    if (sentinel && typeof IntersectionObserver !== "undefined") {
      observer = new IntersectionObserver(
        (entries) => {
          for (const e of entries) {
            if (e.isIntersecting && hasMore && !loadingMore) {
              void loadMore();
            }
          }
        },
        { rootMargin: "200px 0px" },
      );
      observer.observe(sentinel);
    }
  });

  onDestroy(() => {
    observer?.disconnect();
  });
</script>

<section class="list">
  {#if pendingCount > 0 && !searchActive}
    <button class="banner" onclick={mergePending}>
      {pendingCount === 1 ? "1 new message" : `${pendingCount} new messages`} — click to show
    </button>
  {/if}
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
    <div bind:this={sentinel}></div>
    <div class="more">
      {#if loadingMore}
        <span class="hint">Loading more…</span>
      {:else if hasMore}
        <button onclick={loadMore}>Load more</button>
      {:else}
        <span class="hint">End of list</span>
      {/if}
    </div>
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
    color: #666;
    font-size: 12px;
  }
  .error {
    padding: 12px;
    color: #b00;
    font-size: 13px;
  }
  .banner {
    display: block;
    width: 100%;
    border: none;
    background: #e8f0fe;
    color: #1a73e8;
    padding: 8px 12px;
    font-size: 13px;
    cursor: pointer;
    text-align: center;
  }
  .more {
    padding: 12px;
    text-align: center;
  }
  .more button {
    padding: 6px 14px;
    font-size: 13px;
    cursor: pointer;
  }
</style>
