<script lang="ts">
  /**
   * Top-level screen for the logged-in phase. Three-pane Layout-A:
   * [AccountTree | Splitter | MessageList | Splitter | ReadingPane] with a
   * small header bar.
   *
   * On mount we kick off two parallel loads: the account list (drives the
   * tree) and the recent messages list (seeds the middle pane). Both go
   * through the `mail` store so other components observe the same state.
   * Pane widths are persisted in localStorage; change-polling starts on
   * mount and stops on unmount/logout. VersionGate is mounted at the top
   * so a server major mismatch surfaces before the user interacts.
   */
  import { onMount, onDestroy } from "svelte";
  import AccountTree from "../components/AccountTree.svelte";
  import MessageList from "../components/MessageList.svelte";
  import ReadingPane from "../components/ReadingPane.svelte";
  import SearchBar from "../components/SearchBar.svelte";
  import ActiveFilterChips from "../components/ActiveFilterChips.svelte";
  import Splitter from "../components/Splitter.svelte";
  import VersionGate from "../components/VersionGate.svelte";
  import SettingsScreen from "./SettingsScreen.svelte";
  import {
    DEFAULT_LEFT_WIDTH_PX,
    DEFAULT_MIDDLE_WIDTH_PX,
    clampPaneWidths,
    parseStoredWidths,
    serializeWidths,
    type PaneWidths,
  } from "../lib/splitter";
  import { auth } from "../lib/stores/auth.svelte";
  import { mail } from "../lib/stores/mail.svelte";

  const PANE_WIDTHS_KEY = "localmail.gui.paneWidths";

  let pending: boolean = $state(false);
  let settingsOpen: boolean = $state(false);
  let widths: PaneWidths = $state(loadInitialWidths());
  let containerWidth: number = $state(
    typeof window !== "undefined" ? window.innerWidth : 1024,
  );

  function loadInitialWidths(): PaneWidths {
    if (typeof window === "undefined") {
      return { left: DEFAULT_LEFT_WIDTH_PX, middle: DEFAULT_MIDDLE_WIDTH_PX };
    }
    const raw = window.localStorage.getItem(PANE_WIDTHS_KEY);
    if (raw === null) return { left: DEFAULT_LEFT_WIDTH_PX, middle: DEFAULT_MIDDLE_WIDTH_PX };
    return parseStoredWidths(raw) ?? { left: DEFAULT_LEFT_WIDTH_PX, middle: DEFAULT_MIDDLE_WIDTH_PX };
  }

  function persistWidths(w: PaneWidths): void {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(PANE_WIDTHS_KEY, serializeWidths(w));
  }

  function onLeftResize(dx: number): void {
    const next = clampPaneWidths({ left: widths.left + dx, middle: widths.middle }, { containerWidth });
    widths = next;
    persistWidths(next);
  }

  function onMiddleResize(dx: number): void {
    const next = clampPaneWidths({ left: widths.left, middle: widths.middle + dx }, { containerWidth });
    widths = next;
    persistWidths(next);
  }

  function onWindowResize(): void {
    containerWidth = window.innerWidth;
    const clamped = clampPaneWidths(widths, { containerWidth });
    if (clamped.left !== widths.left || clamped.middle !== widths.middle) {
      widths = clamped;
      persistWidths(clamped);
    }
  }

  onMount(async () => {
    if (typeof window !== "undefined") {
      window.addEventListener("resize", onWindowResize);
    }
    await Promise.all([mail.loadAccounts(), mail.loadRecentMessages()]);
    mail.startPolling();
  });

  onDestroy(() => {
    mail.stopPolling();
    if (typeof window !== "undefined") {
      window.removeEventListener("resize", onWindowResize);
    }
  });

  async function onLogout(): Promise<void> {
    pending = true;
    try {
      mail.stopPolling();
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

<VersionGate />

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
        <button
          aria-label="Settings"
          title="Settings"
          data-testid="open-settings"
          onclick={() => (settingsOpen = true)}
          disabled={pending}
        >⚙</button>
        <button onclick={onRefresh} disabled={pending}>Refresh token</button>
        <button onclick={onLogout} disabled={pending}>Log out</button>
      </div>
    </header>
    <SettingsScreen open={settingsOpen} onClose={() => (settingsOpen = false)} />
    <SearchBar />
    <ActiveFilterChips />
    <main
      class="panes"
      style="grid-template-columns: {widths.left}px auto {widths.middle}px auto 1fr;"
    >
      <AccountTree />
      <Splitter onResize={onLeftResize} />
      <MessageList />
      <Splitter onResize={onMiddleResize} />
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
    height: 100%;
    min-height: 0;
  }
</style>
