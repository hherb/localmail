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
  import AdminView from "./AdminView.svelte";
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
  import { version } from "../lib/stores/version.svelte";

  const PANE_WIDTHS_KEY = "localmail.gui.paneWidths";

  let pending: boolean = $state(false);
  let settingsOpen: boolean = $state(false);
  let adminOpen: boolean = $state(false);
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
    try {
      window.localStorage.setItem(PANE_WIDTHS_KEY, serializeWidths(w));
    } catch {
      // QuotaExceededError or Safari private-mode SecurityError. Drag tick
      // should not crash the UI just because persistence failed.
    }
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
    // Block data flow on an api_major mismatch: hitting an incompatible
    // server every 30s churns the network and risks misinterpreting payloads.
    // VersionGate's overlay shows the same modal regardless of where check()
    // is initiated; making MainView own the await keeps the gate honest.
    await version.check();
    if (version.snapshot.compatible === false) return;
    await Promise.all([mail.loadAccounts(), mail.loadInitialMessages()]);
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
        <span class="brand-mark" aria-hidden="true"><span></span></span>
        <div class="brand-copy">
          <strong>localmail</strong>
          <span>Private archive</span>
        </div>
        <div class="account-badge" title="Connected session">
          <span class="online" aria-hidden="true"></span>
          <span class="username">{snap.username}</span>
        </div>
      </div>
      <div class="right">
        <ul class="caps" aria-label="Server capabilities">
          <li class="cap" class:on={snap.capabilities.search}>search</li>
          <li class="cap" class:on={snap.capabilities.attachments}>attachments</li>
          <li class="cap" class:on={snap.capabilities.attachment_text}>attachment_text</li>
          <li class="cap" class:on={snap.capabilities.threading}>threading</li>
          <li class="cap" class:on={snap.capabilities.send}>send</li>
        </ul>
        {#if snap.isAdmin}
          <button
            class="toolbar-button"
            aria-label="Admin"
            title="Admin"
            data-testid="open-admin"
            onclick={() => (adminOpen = true)}
            disabled={pending}
          >Admin</button>
        {/if}
        <button
          class="toolbar-button"
          aria-label="Settings"
          title="Settings"
          data-testid="open-settings"
          onclick={() => (settingsOpen = true)}
          disabled={pending}
        >Settings</button>
        <button class="toolbar-button" onclick={onRefresh} disabled={pending}>Refresh session</button>
        <button class="logout" onclick={onLogout} disabled={pending}>Log out</button>
      </div>
    </header>
    <SettingsScreen open={settingsOpen} onClose={() => (settingsOpen = false)} />
    <AdminView open={adminOpen} onClose={() => (adminOpen = false)} />
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
    background: var(--canvas);
  }
  .bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    min-height: 58px;
    padding: 8px 14px;
    background: rgba(255, 255, 255, 0.96);
    border-bottom: 1px solid var(--border);
    box-shadow: var(--shadow-sm);
    font-size: 12px;
    z-index: 20;
  }
  .left {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .brand-mark {
    position: relative;
    display: grid;
    place-items: center;
    width: 34px;
    height: 34px;
    flex: 0 0 34px;
    border-radius: 10px;
    background: linear-gradient(145deg, #7272e8, #4b4bc3);
    box-shadow: 0 6px 14px rgba(74, 74, 194, 0.24);
  }
  .brand-mark::before {
    content: "";
    width: 17px;
    height: 12px;
    border: 1.5px solid white;
    border-radius: 3px;
  }
  .brand-mark::after {
    content: "";
    position: absolute;
    top: 11px;
    width: 10px;
    height: 10px;
    border-left: 1.5px solid white;
    border-bottom: 1.5px solid white;
    transform: rotate(-45deg);
  }
  .brand-copy {
    display: grid;
    min-width: 90px;
    line-height: 1.15;
  }
  .brand-copy strong {
    font-size: 15px;
    letter-spacing: -0.02em;
  }
  .brand-copy > span {
    color: var(--fg-muted);
    font-size: 10px;
  }
  .account-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-left: 6px;
    padding: 5px 9px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface-subtle);
  }
  .online {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #43a477;
    box-shadow: 0 0 0 3px #e3f5eb;
  }
  .username {
    color: var(--fg);
    font-weight: 600;
  }
  .right {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }
  .caps {
    list-style: none;
    padding: 0;
    margin: 0 6px 0 0;
    display: flex;
    gap: 4px;
  }
  .cap {
    padding: 3px 7px;
    border-radius: 999px;
    background: #f1f2f5;
    color: var(--fg-faint);
    font-size: 10px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    text-decoration: line-through;
  }
  .cap.on {
    background: var(--success-soft);
    color: var(--success);
    text-decoration: none;
  }
  button {
    min-height: 32px;
    padding: 5px 10px;
    font-size: 12px;
  }
  .toolbar-button {
    background: transparent;
    border-color: transparent;
    color: var(--fg-muted);
  }
  .toolbar-button:hover:not(:disabled) {
    background: var(--surface-subtle);
    border-color: var(--border);
    color: var(--fg);
  }
  .logout {
    color: var(--accent-strong);
    border-color: #d8d8ef;
    background: var(--accent-soft);
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .panes {
    display: grid;
    height: 100%;
    min-height: 0;
    margin: 0 8px 8px;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: 0 0 var(--radius-md) var(--radius-md);
    background: var(--surface);
    box-shadow: var(--shadow-sm);
  }

  @media (max-width: 1040px) {
    .caps { display: none; }
    .brand-copy > span { display: none; }
  }
</style>
