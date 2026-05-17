<script lang="ts">
  import { emptyFilters, type SearchFiltersUI } from "../lib/api/search";
  import { extractDslFilters } from "../lib/filter_parse";
  import { search } from "../lib/stores/search.svelte";

  // Seed from a merge of structured filters (already on the search store)
  // and any DSL tokens typed into the search bar — so opening the popover
  // after typing `from:anna` shows "anna" in the From field. DSL tokens
  // that don't map to popover fields stay in the query string as free text.
  function initialLocal(): SearchFiltersUI {
    const dsl = extractDslFilters(search.snapshot.query).filters;
    const stored = search.snapshot.filters;
    return {
      accountIds: stored.accountIds,
      folderIds: stored.folderIds,
      from: stored.from || dsl.from,
      to: stored.to || dsl.to,
      subject: stored.subject || dsl.subject,
      after: stored.after || dsl.after,
      before: stored.before || dsl.before,
      hasAttachment: stored.hasAttachment ?? dsl.hasAttachment,
    };
  }
  let local: SearchFiltersUI = $state(initialLocal());

  async function apply() {
    search.setFilters({
      ...local,
      // Preserve account/folder narrowing the tree has already set.
      accountIds: search.snapshot.filters.accountIds,
      folderIds: search.snapshot.filters.folderIds,
    });
    // Strip any DSL tokens we've absorbed into the popover so they don't
    // duplicate-apply on the server (the server happily ANDs them, but the
    // chips below the bar would render twice — once from popover, once from DSL).
    const { freeText } = extractDslFilters(search.snapshot.query);
    search.setQuery(freeText);
    await search.submit();
  }

  function clear() {
    local = emptyFilters();
  }
</script>

<form class="form" onsubmit={(e) => { e.preventDefault(); void apply(); }}>
  <label for="fp-from">From</label>
  <input id="fp-from" bind:value={local.from} placeholder="anna@" />
  <label for="fp-to">To</label>
  <input id="fp-to" bind:value={local.to} placeholder="horst@" />
  <label for="fp-subject">Subject</label>
  <input id="fp-subject" bind:value={local.subject} placeholder="school" />
  <label for="fp-after">After</label>
  <input id="fp-after" type="date" bind:value={local.after} />
  <label for="fp-before">Before</label>
  <input id="fp-before" type="date" bind:value={local.before} />
  <label for="fp-has-attachment">Has attachment</label>
  <input
    id="fp-has-attachment"
    type="checkbox"
    checked={local.hasAttachment === true}
    onchange={(e) => {
      local.hasAttachment = (e.currentTarget as HTMLInputElement).checked ? true : null;
    }}
  />
  <div class="row">
    <button type="button" onclick={clear}>Clear</button>
    <button type="submit">Apply</button>
  </div>
</form>

<style>
  .form { display: flex; flex-direction: column; gap: 8px; padding: 12px; min-width: 260px; }
  label { font-size: 12px; color: #555; }
  input:not([type="checkbox"]) { padding: 3px 6px; border: 1px solid #ccc; border-radius: 3px; }
  .row { display: flex; justify-content: flex-end; gap: 6px; margin-top: 4px; }
  button { padding: 4px 10px; border: 1px solid #ccc; background: #fff; border-radius: 4px; cursor: pointer; }
</style>
