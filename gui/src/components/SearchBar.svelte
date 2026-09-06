<script lang="ts">
  import { search, type SortMode } from "../lib/stores/search.svelte";
  import {
    displayedSort,
    RELEVANCE_UNAVAILABLE_REASON,
    relevanceUnavailable,
    sortClick,
  } from "../lib/sort_display";

  // Referenced by the Relevance radio's `aria-describedby` when the reason
  // is on screen (#354). A module constant so the two cannot drift.
  const REASON_ID = "sort-relevance-unavailable";

  // The selector renders the ordering that RAN, not the one requested (#345).
  // The server resolves `sort` from the query, so a textless one — an empty
  // box with a filter chip, or a box holding only `from:` / `has:` — is
  // served date-ordered whatever was asked for. Binding the radios to the
  // request made Relevance assert an ordering that was not in effect —
  // and clicking it either re-ran the search to no effect (from an explicit
  // Date selection) or fired no event at all (from the default state).
  const shownSort = $derived(
    displayedSort(search.snapshot.sort, search.snapshot.sortApplied),
  );
  // Read off the server's own answer rather than inferred from the request
  // (#353): `sort_applied` is `date` both for a query with nothing to rank
  // and for a text query whose caller chose date, so inferring re-enabled
  // Relevance the moment a Date click was recorded.
  const rankUnavailable = $derived(
    relevanceUnavailable(search.snapshot.rankable),
  );

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

  // A click asks two questions of two different fields, and reading one
  // field for both is #353: the radios show what *ran* while the guard
  // compared the stored preference, so clicking the already-checked Date
  // after a textless search recorded nothing.
  //
  // Bound to `click`, not `change`, and that is the whole of the DOM half
  // of the fix. A radio fires no `change` at all when it is already
  // checked, which is exactly the #353 state; it fires `click` either way,
  // for the pointer and for keyboard activation alike. So `click` is a
  // strict superset and binding **both** is wrong — measured: the two
  // handlers each see a `shownSort` that only moves when the response
  // lands, so a real change of mind fired two searches.
  //
  // Re-running still only happens when a search is already on screen
  // (tookMs !== null) — toggling pre-search stores the preference for the
  // next submit rather than firing a request the user did not ask for.
  async function onSortChange(next: SortMode): Promise<void> {
    const { record, resubmit } = sortClick({
      preference: search.snapshot.sort,
      shown: shownSort,
      clicked: next,
    });
    if (record) search.setSort(next);
    if (resubmit && search.snapshot.tookMs !== null) {
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
    <label class:unavailable={rankUnavailable}>
      <input
        type="radio"
        name="sort"
        value="rank"
        checked={shownSort === "rank"}
        onclick={() => onSortChange("rank")}
        disabled={search.snapshot.loading || rankUnavailable}
        aria-describedby={rankUnavailable ? REASON_ID : undefined}
      />
      Relevance
    </label>
    <label>
      <input
        type="radio"
        name="sort"
        value="date"
        checked={shownSort === "date"}
        onclick={() => onSortChange("date")}
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

<!--
  The reason Relevance is disabled, as text rather than a `title` (#354).
  A disabled input is out of the tab order, so a tooltip could not be
  reached by keyboard at all, and `title` is announced inconsistently by
  screen readers and is hover-only for pointer users. This follows the
  precedent already set for a server-disabled control in this codebase:
  AccountForm's `.hint` span and DaemonPanel's `.note` paragraph both render
  their reasons into the markup.
-->
{#if rankUnavailable}
  <p class="sort-note" id={REASON_ID} data-testid="relevance-unavailable">
    {RELEVANCE_UNAVAILABLE_REASON}
  </p>
{/if}

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
  fieldset.sort label.unavailable {
    opacity: 0.45;
    cursor: not-allowed;
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
  .sort-note {
    margin: 0;
    padding: 6px 14px 8px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    color: var(--fg-muted);
  }
  .popover {
    position: absolute; right: 14px; top: 108px; background: var(--surface);
    border: 1px solid var(--border); border-radius: var(--radius-md);
    box-shadow: 0 16px 42px rgba(31, 35, 61, 0.18);
    z-index: 80;
  }
</style>
