<script lang="ts">
  /**
   * Top-level screen for the logged-in phase. Three-pane Layout-A:
   * [AccountTree | MessageList | ReadingPane] with a small header bar.
   *
   * On mount we kick off two parallel loads: the account list (drives the
   * tree) and the recent messages list (seeds the middle pane). Both go
   * through the `mail` store so other components observe the same state.
   */
  import { onMount } from "svelte";
  import AccountTree from "../components/AccountTree.svelte";
  import MessageList from "../components/MessageList.svelte";
  import ReadingPane from "../components/ReadingPane.svelte";
  import SearchBar from "../components/SearchBar.svelte";
  import ActiveFilterChips from "../components/ActiveFilterChips.svelte";
  import { auth } from "../lib/stores/auth.svelte";
  import { mail } from "../lib/stores/mail.svelte";

  let pending: boolean = $state(false);

  onMount(async () => {
    await Promise.all([mail.loadAccounts(), mail.loadRecentMessages()]);
  });

  async function onLogout(): Promise<void> {
    pending = true;
    try {
      mail.reset();
      await auth.logout();
    } finally {
      pending = false;
    }
  }

  async function onRefresh(): Promise<void> {
    pending = true;
    try {
      await auth.refreshToken();
    } finally {
      pending = false;
    }
  }
</script>

{#if auth.snapshot.phase === "logged_in"}
  {@const snap = auth.snapshot}
  <div class="app">
    <header class="bar">
      <div class="left">
        <strong>localmail</strong>
        <span class="username">{snap.username}</span>
      </div>
      <div class="right">
        <ul class="caps">
          <li class="cap" class:on={snap.capabilities.search}>search</li>
          <li class="cap" class:on={snap.capabilities.attachments}>attachments</li>
          <li class="cap" class:on={snap.capabilities.attachment_text}>attachment_text</li>
          <li class="cap" class:on={snap.capabilities.threading}>threading</li>
          <li class="cap" class:on={snap.capabilities.send}>send</li>
        </ul>
        <button onclick={onRefresh} disabled={pending}>Refresh token</button>
        <button onclick={onLogout} disabled={pending}>Log out</button>
      </div>
    </header>
    <SearchBar />
    <ActiveFilterChips />
    <main class="panes">
      <AccountTree />
      <MessageList />
      <ReadingPane />
    </main>
  </div>
{/if}

<style>
  .app {
    height: 100vh;
    display: grid;
    grid-template-rows: auto auto auto 1fr;
  }
  .bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 12px;
    background: #f4f6f9;
    border-bottom: 1px solid #e0e3e8;
    font-size: 12px;
  }
  .left {
    display: flex;
    align-items: baseline;
    gap: 12px;
  }
  .username {
    color: #1a4fc7;
    font-weight: 600;
  }
  .right {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .caps {
    list-style: none;
    padding: 0;
    margin: 0 8px 0 0;
    display: flex;
    gap: 4px;
  }
  .cap {
    padding: 2px 8px;
    border-radius: 10px;
    background: #ececec;
    color: #888;
    font-size: 11px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    text-decoration: line-through;
  }
  .cap.on {
    background: #e6f5dd;
    color: #2d6a1a;
    text-decoration: none;
  }
  button {
    padding: 3px 10px;
    font-size: 12px;
    background: #fff;
    border: 1px solid #ccc;
    border-radius: 4px;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .panes {
    display: grid;
    grid-template-columns: 220px 340px 1fr;
    height: 100%;
    min-height: 0;
  }
</style>
