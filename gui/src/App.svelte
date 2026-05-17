<script lang="ts">
  import { greet, type Greeting } from "./lib/tauri";

  let greeting: Greeting | null = $state(null);
  let name: string = $state("world");
  let error: string | null = $state(null);

  async function onGreet(): Promise<void> {
    error = null;
    greeting = null;
    try {
      greeting = await greet(name);
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : String(e);
    }
  }
</script>

<main class="container">
  <h1>localmail</h1>
  <p class="tagline">read-only archive browser — scaffolding only</p>

  <form
    onsubmit={(e: Event) => {
      e.preventDefault();
      void onGreet();
    }}
  >
    <input id="greet-input" bind:value={name} placeholder="who?" />
    <button type="submit">Greet from Rust</button>
  </form>

  {#if greeting}
    <p class="greeting">{greeting.message}</p>
    <p class="meta">via {greeting.source}</p>
  {/if}

  {#if error}
    <p class="error">error: {error}</p>
  {/if}
</main>

<style>
  .meta {
    margin: 4px 0 0;
    font-size: 11px;
    color: #888;
  }
  .error {
    margin-top: 16px;
    padding: 12px;
    background: #fdecea;
    border-left: 3px solid #c0392b;
    color: #c0392b;
    font-family: ui-monospace, SFMono-Regular, monospace;
  }
</style>
