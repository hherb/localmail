<script lang="ts">
  import { search, type SortMode } from "../lib/stores/search.svelte";

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

  // Switching sort mode only re-runs the search when one is already on
  // screen (tookMs !== null). Otherwise the toggle just stores the user's
  // preference for their next submit — toggling pre-search shouldn't fire
  // a request the user didn't ask for.
  async function onSortChange(next: SortMode): Promise<void> {
    if (search.snapshot.sort === next) return;
    search.setSort(next);
    if (search.snapshot.tookMs !== null) {
      await search.submit();
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
  <div class="search-field">
    <span class="search-icon" aria-hidden="true"></span>
    <input
      type="search"
      aria-label="Search mail"
      placeholder="Search messages, people, or use from: and has:attachment"
      value={search.snapshot.query}
      oninput={(e) => search.setQuery((e.currentTarget as HTMLInputElement).value)}
      onkeydown={onKeyDown}
      disabled={search.snapshot.loading}
    />
  </div>
  <fieldset class="sort" aria-label="Sort results by">
    <label>
      <input
        type="radio"
        name="sort"
        value="rank"
        checked={search.snapshot.sort === "rank"}
        onchange={() => onSortChange("rank")}
        disabled={search.snapshot.loading}
      />
      Relevance
    </label>
    <label>
      <input
        type="radio"
        name="sort"
        value="date"
        checked={search.snapshot.sort === "date"}
        onchange={() => onSortChange("date")}
        disabled={search.snapshot.loading}
      />
      Date
    </label>
  </fieldset>
  <button class="primary" type="submit" disabled={search.snapshot.loading}>
    {search.snapshot.loading ? "Searching…" : "Search"}
  </button>
  <button class:active={popoverOpen} type="button" bind:this={filterBtnEl} onclick={togglePopover}>
    Filters
  </button>
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
    display: flex; gap: 8px; padding: 10px 14px;
    background: var(--surface); border-bottom: 1px solid var(--border);
    align-items: center;
  }
  .search-field {
    position: relative;
    flex: 1;
  }
  .search-icon {
    position: absolute;
    left: 13px;
    top: 50%;
    width: 14px;
    height: 14px;
    border: 1.8px solid var(--fg-faint);
    border-radius: 50%;
    transform: translateY(-56%);
    pointer-events: none;
  }
  .search-icon::after {
    content: "";
    position: absolute;
    width: 6px;
    border-top: 1.8px solid var(--fg-faint);
    right: -5px;
    bottom: -3px;
    transform: rotate(45deg);
  }
  input[type="search"] {
    width: 100%;
    min-height: 40px;
    padding: 8px 12px 8px 38px;
    border-color: var(--border);
    border-radius: 10px;
    background: var(--surface-subtle);
  }
  fieldset.sort {
    display: flex; gap: 2px; align-items: center;
    border: 1px solid var(--border); padding: 3px; margin: 0;
    border-radius: 9px; background: var(--surface-subtle);
    font-size: 11px; color: var(--fg-muted);
  }
  fieldset.sort label {
    position: relative;
    display: inline-flex; align-items: center; gap: 4px; cursor: pointer;
    padding: 5px 8px;
    border-radius: 6px;
  }
  fieldset.sort label:has(input:checked) {
    background: var(--surface);
    color: var(--fg);
    box-shadow: var(--shadow-sm);
  }
  fieldset.sort input[type="radio"] {
    position: absolute;
    opacity: 0;
    pointer-events: none;
  }
  button {
    min-height: 38px;
    padding: 7px 12px;
  }
  button.primary {
    min-width: 78px;
    border-color: var(--accent);
    background: var(--accent);
    color: white;
  }
  button.primary:hover:not(:disabled) {
    border-color: var(--accent-hover);
    background: var(--accent-hover);
  }
  button.active {
    border-color: #bdbdeb;
    background: var(--accent-soft);
    color: var(--accent-strong);
  }
  .popover {
    position: absolute; right: 14px; top: 108px; background: var(--surface);
    border: 1px solid var(--border); border-radius: var(--radius-md);
    box-shadow: 0 16px 42px rgba(31, 35, 61, 0.18);
    z-index: 80;
  }
</style>
