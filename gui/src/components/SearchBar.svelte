<script lang="ts">
  import { search } from "../lib/stores/search.svelte";

  let popoverOpen = $state(false);
  let popoverEl: HTMLDivElement | undefined = $state();
  let filterBtnEl: HTMLButtonElement | undefined = $state();

  async function onSubmit(e?: Event) {
    e?.preventDefault();
    await search.submit();
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      void onSubmit();
    }
  }

  function togglePopover() { popoverOpen = !popoverOpen; }
  function closePopover() { popoverOpen = false; }

  function onDocumentClick(e: MouseEvent) {
    if (!popoverOpen) return;
    const t = e.target as Node | null;
    if (!t) return;
    if (popoverEl?.contains(t)) return;
    if (filterBtnEl?.contains(t)) return;
    closePopover();
  }

  function onDocumentKey(e: KeyboardEvent) {
    if (popoverOpen && e.key === "Escape") closePopover();
  }
</script>

<svelte:window onclick={onDocumentClick} onkeydown={onDocumentKey} />

<form class="bar" onsubmit={onSubmit}>
  <input
    type="search"
    placeholder="Search across all accounts"
    value={search.snapshot.query}
    oninput={(e) => search.setQuery((e.currentTarget as HTMLInputElement).value)}
    onkeydown={onKeyDown}
    disabled={search.snapshot.loading}
  />
  <button type="submit" disabled={search.snapshot.loading}>Search</button>
  <button type="button" bind:this={filterBtnEl} onclick={togglePopover}>🔧 Filters</button>
</form>

{#if popoverOpen}
  <div class="popover" role="dialog" bind:this={popoverEl}>
    {#await import("./FilterPopover.svelte") then mod}
      {@const C = mod.default}
      <C onClose={closePopover} />
    {/await}
  </div>
{/if}

<style>
  .bar {
    display: flex; gap: 6px; padding: 6px 12px;
    background: #fafbfd; border-bottom: 1px solid #e0e3e8;
  }
  input {
    flex: 1; padding: 4px 8px; border: 1px solid #ccc; border-radius: 4px;
  }
  button {
    padding: 4px 10px; background: #fff; border: 1px solid #ccc;
    border-radius: 4px; cursor: pointer;
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .popover {
    position: absolute; right: 12px; top: 40px; background: #fff;
    border: 1px solid #ccc; padding: 12px; border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
  }
</style>
