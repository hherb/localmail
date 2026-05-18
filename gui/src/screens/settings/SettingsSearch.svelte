<script lang="ts">
  /**
   * Search tab: results-per-page, default search language, and the debug
   * toggle that surfaces per-result scores and matched arms. Text inputs
   * commit on blur — the store's `set*` methods clamp/normalise, so we
   * mirror the canonical value back into the local state after each commit.
   */
  import { settings } from "../../lib/stores/settings.svelte";

  let pageSizeText: string = $state(String(settings.snapshot.pageSize));
  let langText: string = $state(settings.snapshot.defaultLanguage ?? "");

  function applyPageSize(): void {
    const n = Number(pageSizeText);
    if (Number.isFinite(n) && n > 0) settings.setPageSize(n);
    pageSizeText = String(settings.snapshot.pageSize);
  }

  function applyLanguage(): void {
    settings.setDefaultLanguage(langText.trim() || null);
    langText = settings.snapshot.defaultLanguage ?? "";
  }
</script>

<section class="search">
  <h3>Page size</h3>
  <label>
    Results per search:
    <input
      type="number"
      min="1"
      max="200"
      bind:value={pageSizeText}
      onblur={applyPageSize}
    />
  </label>

  <h3>Default language</h3>
  <label>
    ISO 639-1 code (or empty for none):
    <input
      type="text"
      maxlength="5"
      bind:value={langText}
      onblur={applyLanguage}
      placeholder="e.g. en"
    />
  </label>

  <h3>Debug</h3>
  <label>
    <input
      type="checkbox"
      checked={settings.snapshot.debug}
      onchange={(e) => settings.setDebug((e.currentTarget as HTMLInputElement).checked)}
    />
    Show per-result scores, matched arms, and chunk highlights
  </label>
</section>

<style>
  label {
    display: block;
    margin: 0.25rem 0;
  }
  h3 {
    margin-top: 1rem;
  }
</style>
