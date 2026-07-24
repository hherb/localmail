<script lang="ts">
  /**
   * Full-pane admin overlay, mounted over MainView and revealed only for
   * is_admin users. Mirrors SettingsScreen's modal + tab structure so the
   * two overlays behave identically. Each tab body renders only when
   * active, keeping the DOM small and tab assertions trivial.
   *
   * Only the Accounts panel is implemented; the remaining three are
   * placeholders until their own phases land.
   */
  type Tab = "accounts" | "daemon" | "users" | "imports";

  interface Props {
    open: boolean;
    onClose: () => void;
  }
  let { open, onClose }: Props = $props();

  let tab: Tab = $state("accounts");
</script>

{#if open}
  <div class="overlay" role="dialog" aria-modal="true" aria-labelledby="admin-title">
    <div class="modal">
      <header>
        <h2 id="admin-title">Admin</h2>
        <button class="close" onclick={onClose} aria-label="Close">×</button>
      </header>
      <div class="tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "accounts"}
          class:active={tab === "accounts"}
          data-testid="admin-tab-accounts"
          onclick={() => (tab = "accounts")}
        >Accounts</button>
        <button
          role="tab"
          aria-selected={tab === "daemon"}
          class:active={tab === "daemon"}
          data-testid="admin-tab-daemon"
          onclick={() => (tab = "daemon")}
        >Daemon</button>
        <button
          role="tab"
          aria-selected={tab === "users"}
          class:active={tab === "users"}
          data-testid="admin-tab-users"
          onclick={() => (tab = "users")}
        >Users</button>
        <button
          role="tab"
          aria-selected={tab === "imports"}
          class:active={tab === "imports"}
          data-testid="admin-tab-imports"
          onclick={() => (tab = "imports")}
        >Imports</button>
      </div>
      <section class="body" role="tabpanel" data-testid="admin-panel-body">
        {#if tab === "accounts"}
          <p class="placeholder">Accounts panel</p>
        {/if}
        {#if tab === "daemon"}
          <p class="placeholder">Daemon control is not available in this build yet.</p>
        {/if}
        {#if tab === "users"}
          <p class="placeholder">User management is not available in this build yet.</p>
        {/if}
        {#if tab === "imports"}
          <p class="placeholder">Archive imports are not available in this build yet.</p>
        {/if}
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
    width: min(960px, 94vw);
    height: min(680px, 92vh);
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
  .placeholder {
    color: #666;
    font-size: 0.9rem;
  }
</style>
