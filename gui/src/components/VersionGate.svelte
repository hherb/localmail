<script lang="ts">
  /**
   * Hard modal shown when the server's api_major doesn't match what this
   * client was built against. Single action: [Quit]. The user must run
   * `localmail serve` of a compatible version, or download a matching
   * client release.
   */
  import { onMount } from "svelte";
  import { version } from "../lib/stores/version.svelte";
  import { invoke } from "@tauri-apps/api/core";

  onMount(() => {
    void version.check();
  });

  async function onQuit(): Promise<void> {
    try {
      await invoke("quit_app_cmd");
    } catch {
      window.close();
    }
  }
</script>

{#if version.snapshot.compatible === false}
  <div class="overlay" role="dialog" aria-modal="true" aria-labelledby="vg-title">
    <div class="modal">
      <h2 id="vg-title">Incompatible server</h2>
      <p>
        This client expects API major 1; the server reports
        {version.snapshot.info?.api_major ?? "?"}.
        Update one of them, then retry.
      </p>
      <button onclick={onQuit}>Quit</button>
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: grid;
    place-items: center;
    z-index: 1000;
  }
  .modal {
    background: white;
    padding: 1.5rem;
    border-radius: 6px;
    max-width: 480px;
  }
</style>
