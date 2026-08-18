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
  <div class="section-heading">
    <h3>Search</h3>
    <p>Defaults apply to new searches and browse requests immediately.</p>
  </div>

  <div class="setting-card field-row">
    <div>
      <label for="settings-page-size">Results per page</label>
      <p>Higher values reduce paging but may take longer on remote servers.</p>
    </div>
    <div class="number-wrap">
      <input
        id="settings-page-size"
        type="number"
        min="1"
        max="200"
        bind:value={pageSizeText}
        onblur={applyPageSize}
      />
      <span>max 200</span>
    </div>
  </div>

  <div class="setting-card field-row">
    <div>
      <label for="settings-language">Default language filter</label>
      <p>Optional BCP-47 tag used when a search does not specify its own language.</p>
    </div>
    <input
      id="settings-language"
      class="language"
      type="text"
      maxlength="8"
      bind:value={langText}
      onblur={applyLanguage}
      placeholder="e.g. en or en-GB"
    />
  </div>

  <div class="setting-card toggle-row">
    <div>
      <label for="settings-debug">Search diagnostics</label>
      <p>Show scores, matched search arms, and extracted chunk highlights.</p>
    </div>
    <label class="switch" aria-label="Search diagnostics">
      <input
        id="settings-debug"
        type="checkbox"
        checked={settings.snapshot.debug}
        onchange={(e) => settings.setDebug((e.currentTarget as HTMLInputElement).checked)}
      />
      <span></span>
    </label>
  </div>
</section>

<style>
  .search { display: grid; gap: 14px; }
  .section-heading h3 { margin: 0; font-size: 18px; letter-spacing: -0.02em; }
  .section-heading p, .setting-card p {
    margin: 4px 0 0;
    color: var(--fg-muted);
    font-size: 12px;
  }
  .setting-card {
    padding: 15px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-subtle);
  }
  .field-row, .toggle-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 24px;
  }
  .field-row > div:first-child, .toggle-row > div { min-width: 0; }
  label { color: var(--fg); font-size: 13px; font-weight: 650; }
  .number-wrap { display: grid; justify-items: end; flex: 0 0 100px; }
  .number-wrap input { width: 100px; text-align: right; }
  .number-wrap span { margin-top: 3px; color: var(--fg-faint); font-size: 10px; }
  input.language { width: 170px; flex: 0 0 170px; }
  .switch { position: relative; flex: 0 0 42px; width: 42px; height: 24px; cursor: pointer; }
  .switch input { position: absolute; opacity: 0; }
  .switch span {
    position: absolute;
    inset: 0;
    border-radius: 999px;
    background: #cbd0dc;
    transition: background 140ms ease;
  }
  .switch span::after {
    content: "";
    position: absolute;
    top: 3px;
    left: 3px;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: white;
    box-shadow: 0 1px 3px rgba(31, 35, 61, 0.25);
    transition: transform 140ms ease;
  }
  .switch input:checked + span { background: var(--accent); }
  .switch input:checked + span::after { transform: translateX(18px); }
  .switch input:focus-visible + span { outline: 2px solid var(--accent); outline-offset: 2px; }
  @media (max-width: 780px) {
    .field-row { align-items: flex-start; flex-direction: column; gap: 10px; }
  }
</style>
