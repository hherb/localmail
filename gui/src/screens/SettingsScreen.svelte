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
</script>

{#if open}
  <div class="overlay" role="dialog" aria-modal="true" aria-labelledby="settings-title">
    <div class="modal">
      <header>
        <h2 id="settings-title">Settings</h2>
        <button class="close" onclick={onClose} aria-label="Close">×</button>
      </header>
      <div class="tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "server"}
          class:active={tab === "server"}
          data-testid="settings-tab-server"
          onclick={() => (tab = "server")}
        >Server</button>
        <button
          role="tab"
          aria-selected={tab === "display"}
          class:active={tab === "display"}
          data-testid="settings-tab-display"
          onclick={() => (tab = "display")}
        >Display</button>
        <button
          role="tab"
          aria-selected={tab === "search"}
          class:active={tab === "search"}
          data-testid="settings-tab-search"
          onclick={() => (tab = "search")}
        >Search</button>
        <button
          role="tab"
          aria-selected={tab === "about"}
          class:active={tab === "about"}
          data-testid="settings-tab-about"
          onclick={() => (tab = "about")}
        >About</button>
      </div>
      <section class="body" role="tabpanel">
        {#if tab === "server"}<SettingsServer />{/if}
        {#if tab === "display"}<SettingsDisplay />{/if}
        {#if tab === "search"}<SettingsSearch />{/if}
        {#if tab === "about"}<SettingsAbout />{/if}
      </section>
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: grid;
    place-items: center;
    z-index: 200;
  }
  .modal {
    background: white;
    width: min(800px, 90vw);
    height: min(600px, 90vh);
    display: flex;
    flex-direction: column;
    border-radius: 6px;
    overflow: hidden;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid #ddd;
  }
  header h2 {
    margin: 0;
    font-size: 1.05rem;
  }
  .close {
    font-size: 1.25rem;
    background: none;
    border: none;
    cursor: pointer;
    line-height: 1;
    padding: 0 0.5rem;
  }
  .tabs {
    display: flex;
    gap: 0.25rem;
    padding: 0 1rem;
    border-bottom: 1px solid #ddd;
  }
  .tabs button {
    padding: 0.5rem 0.75rem;
    background: none;
    border: none;
    cursor: pointer;
    font-size: 0.9rem;
    border-bottom: 2px solid transparent;
  }
  .tabs button.active {
    font-weight: 600;
    border-bottom-color: #1a73e8;
    color: #1a73e8;
  }
  .body {
    flex: 1;
    padding: 1rem;
    overflow: auto;
  }
</style>
