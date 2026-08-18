<script lang="ts">
  /**
   * Full-pane settings overlay. Mounted as an absolutely-positioned layer
   * over MainView; the parent toggles visibility via `open` prop. Tabs are
   * sub-components so each can have its own state + test isolation.
   *
   * Each tab body renders only when active — keeps DOM small and makes
   * test assertions about which tab is showing trivial.
   */
  import SettingsServer from "./settings/SettingsServer.svelte";
  import SettingsDisplay from "./settings/SettingsDisplay.svelte";
  import SettingsSearch from "./settings/SettingsSearch.svelte";
  import SettingsAbout from "./settings/SettingsAbout.svelte";

  type Tab = "server" | "display" | "search" | "about";

  interface Props {
    open: boolean;
    onClose: () => void;
  }
  let { open, onClose }: Props = $props();

  let tab: Tab = $state("server");

  function onKeydown(event: KeyboardEvent): void {
    if (open && event.key === "Escape") onClose();
  }

  function onBackdrop(event: MouseEvent): void {
    if (event.target === event.currentTarget) onClose();
  }
</script>

<svelte:window onkeydown={onKeydown} />

{#if open}
  <div
    class="overlay"
    role="dialog"
    aria-modal="true"
    aria-labelledby="settings-title"
    tabindex="-1"
    onclick={onBackdrop}
    onkeydown={onKeydown}
  >
    <div class="modal">
      <header>
        <div>
          <p class="eyebrow">Preferences</p>
          <h2 id="settings-title">Settings</h2>
        </div>
        <span class="saved">Changes save automatically</span>
        <button class="close" onclick={onClose} aria-label="Close">×</button>
      </header>
      <div class="content">
        <div class="tabs" role="tablist" aria-orientation="vertical">
          <button
            role="tab"
            aria-selected={tab === "server"}
            class:active={tab === "server"}
            data-testid="settings-tab-server"
            onclick={() => (tab = "server")}
          ><span class="tab-icon">S</span><span>Server<small>Connection & security</small></span></button>
          <button
            role="tab"
            aria-selected={tab === "display"}
            class:active={tab === "display"}
            data-testid="settings-tab-display"
            onclick={() => (tab = "display")}
          ><span class="tab-icon">D</span><span>Display<small>Reading preferences</small></span></button>
          <button
            role="tab"
            aria-selected={tab === "search"}
            class:active={tab === "search"}
            data-testid="settings-tab-search"
            onclick={() => (tab = "search")}
          ><span class="tab-icon">F</span><span>Search<small>Results & diagnostics</small></span></button>
          <button
            role="tab"
            aria-selected={tab === "about"}
            class:active={tab === "about"}
            data-testid="settings-tab-about"
            onclick={() => (tab = "about")}
          ><span class="tab-icon">i</span><span>About<small>Versions & logs</small></span></button>
        </div>
        <section class="body" role="tabpanel">
          {#if tab === "server"}<SettingsServer />{/if}
          {#if tab === "display"}<SettingsDisplay />{/if}
          {#if tab === "search"}<SettingsSearch />{/if}
          {#if tab === "about"}<SettingsAbout />{/if}
        </section>
      </div>
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(27, 29, 48, 0.48);
    backdrop-filter: blur(5px);
    display: grid;
    place-items: center;
    z-index: 200;
  }
  .modal {
    background: var(--surface);
    width: min(860px, 92vw);
    height: min(640px, 90vh);
    display: flex;
    flex-direction: column;
    border: 1px solid rgba(255, 255, 255, 0.7);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg);
    overflow: hidden;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    min-height: 78px;
    padding: 14px 18px 14px 22px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  header h2 {
    margin: 0;
    font-size: 1.4rem;
    letter-spacing: -0.025em;
  }
  .eyebrow {
    margin: 0 0 1px;
    color: var(--accent);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .saved {
    margin-left: auto;
    color: var(--fg-faint);
    font-size: 11px;
  }
  .close {
    display: grid;
    place-items: center;
    width: 34px;
    min-height: 34px;
    margin-left: 14px;
    padding: 0;
    font-size: 1.4rem;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    line-height: 1;
    color: var(--fg-muted);
  }
  .content {
    display: grid;
    grid-template-columns: 220px 1fr;
    flex: 1;
    min-height: 0;
  }
  .tabs {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 16px 12px;
    border-right: 1px solid var(--border);
    background: var(--surface-subtle);
  }
  .tabs button {
    display: grid;
    grid-template-columns: 30px 1fr;
    align-items: center;
    gap: 9px;
    min-height: 52px;
    padding: 7px 9px;
    text-align: left;
    background: transparent;
    border-color: transparent;
    color: var(--fg-muted);
  }
  .tabs button.active {
    font-weight: 600;
    border-color: #d9d9f4;
    background: var(--accent-soft);
    color: var(--accent-strong);
  }
  .tab-icon {
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--surface);
    color: var(--fg-muted);
    font-size: 11px;
    font-weight: 700;
  }
  .active .tab-icon {
    border-color: #c3c3ed;
    color: var(--accent);
  }
  .tabs button > span:last-child {
    display: grid;
    font-size: 13px;
  }
  .tabs small {
    color: var(--fg-faint);
    font-size: 10px;
    font-weight: 400;
  }
  .body {
    min-width: 0;
    padding: 22px 26px 28px;
    overflow: auto;
  }
</style>
