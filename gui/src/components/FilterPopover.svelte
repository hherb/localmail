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
      dateFrom: stored.dateFrom || dsl.dateFrom,
      dateTo: stored.dateTo || dsl.dateTo,
      language: stored.language || dsl.language,
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

  function onLanguageInput(e: Event) {
    // ISO 639 codes are lowercase by convention; normalise on entry so the
    // emitted DSL token (`lang:en`) and the wire `lang` field match.
    local.language = (e.currentTarget as HTMLInputElement).value.toLowerCase();
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
  <label for="fp-date-from">From date</label>
  <div class="field">
    <input id="fp-date-from" type="date" bind:value={local.dateFrom} />
    <button
      type="button"
      class="clear-x"
      aria-label="Clear from date"
      onclick={() => { local.dateFrom = ""; }}
    >×</button>
  </div>
  <label for="fp-date-to">To date</label>
  <div class="field">
    <input id="fp-date-to" type="date" bind:value={local.dateTo} />
    <button
      type="button"
      class="clear-x"
      aria-label="Clear to date"
      onclick={() => { local.dateTo = ""; }}
    >×</button>
  </div>
  <label for="fp-language">Language</label>
  <div class="field">
    <input
      id="fp-language"
      type="text"
      maxlength="8"
      placeholder="e.g. en"
      value={local.language ?? ""}
      oninput={onLanguageInput}
    />
    <button
      type="button"
      class="clear-x"
      aria-label="Clear language"
      onclick={() => { local.language = ""; }}
    >×</button>
  </div>
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
  .field { display: flex; gap: 4px; align-items: center; }
  .field input { flex: 1; }
  .clear-x { padding: 0 6px; border: 1px solid #ccc; background: #fff; border-radius: 3px;
             cursor: pointer; font-size: 14px; line-height: 1.2; color: #555; }
  .row { display: flex; justify-content: flex-end; gap: 6px; margin-top: 4px; }
  button { padding: 4px 10px; border: 1px solid #ccc; background: #fff; border-radius: 4px; cursor: pointer; }
</style>
