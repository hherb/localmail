<script lang="ts">
  /**
   * Display tab: density, date format, and the default HTML-image policy.
   * Pure form controls — state lives in the settings store, which persists
   * to localStorage on every mutation.
   */
  import { settings } from "../../lib/stores/settings.svelte";
</script>

<section class="display">
  <div class="section-heading">
    <h3>Display</h3>
    <p>Choose how much information appears and how message content is handled.</p>
  </div>

  <fieldset class="setting-card">
    <legend>Message list density</legend>
    <p>Comfortable is easier to scan; compact fits more messages on screen.</p>
    <div class="choice-grid two">
      <label>
        <input
          type="radio"
          name="density"
          value="comfortable"
          checked={settings.snapshot.density === "comfortable"}
          onchange={() => settings.setDensity("comfortable")}
        />
        <span><strong>Comfortable</strong><small>More space and preview text</small></span>
      </label>
      <label>
        <input
          type="radio"
          name="density"
          value="compact"
          checked={settings.snapshot.density === "compact"}
          onchange={() => settings.setDensity("compact")}
        />
        <span><strong>Compact</strong><small>Denser rows for large archives</small></span>
      </label>
    </div>
  </fieldset>

  <fieldset class="setting-card">
    <legend>Message dates</legend>
    <p>Relative dates stay concise; absolute dates always show the full timestamp.</p>
    <div class="choice-grid two">
      <label>
        <input
          type="radio"
          name="dateFormat"
          value="relative"
          checked={settings.snapshot.dateFormat === "relative"}
          onchange={() => settings.setDateFormat("relative")}
        />
        <span><strong>Relative</strong><small>Today, Mar 3, Dec 25 2024</small></span>
      </label>
      <label>
        <input
          type="radio"
          name="dateFormat"
          value="absolute"
          checked={settings.snapshot.dateFormat === "absolute"}
          onchange={() => settings.setDateFormat("absolute")}
        />
        <span><strong>Absolute</strong><small>Full date and local time</small></span>
      </label>
    </div>
  </fieldset>

  <fieldset class="setting-card">
    <legend>Remote images</legend>
    <p>External images can reveal when you open a message. Blocking is the safest choice.</p>
    <div class="choice-grid three">
      <label>
        <input
          type="radio"
          name="img"
          value="block"
          checked={settings.snapshot.imagePolicy === "block"}
          onchange={() => settings.setImagePolicy("block")}
        />
        <span><strong>Block</strong><small>Never request them</small></span>
      </label>
      <label>
        <input
          type="radio"
          name="img"
          value="ask"
          checked={settings.snapshot.imagePolicy === "ask"}
          onchange={() => settings.setImagePolicy("ask")}
        />
        <span><strong>Ask each time</strong><small>Allow per message</small></span>
      </label>
      <label>
        <input
          type="radio"
          name="img"
          value="allow"
          checked={settings.snapshot.imagePolicy === "allow"}
          onchange={() => settings.setImagePolicy("allow")}
        />
        <span><strong>Always allow</strong><small>Load automatically</small></span>
      </label>
    </div>
  </fieldset>
</section>

<style>
  .display { display: grid; gap: 14px; }
  .section-heading h3 { margin: 0; font-size: 18px; letter-spacing: -0.02em; }
  .section-heading p, .setting-card > p {
    margin: 4px 0 0;
    color: var(--fg-muted);
    font-size: 12px;
  }
  .setting-card {
    margin: 0;
    padding: 15px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-subtle);
  }
  legend { padding: 0; font-weight: 650; font-size: 13px; }
  .choice-grid { display: grid; gap: 8px; margin-top: 12px; }
  .choice-grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .choice-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  label {
    position: relative;
    display: flex;
    min-width: 0;
    padding: 10px 11px;
    border: 1px solid var(--border);
    border-radius: 9px;
    background: var(--surface);
    cursor: pointer;
  }
  label:has(input:checked) {
    border-color: #b9b9ea;
    background: var(--accent-soft);
    box-shadow: inset 0 0 0 1px rgba(91, 91, 214, 0.06);
  }
  input { position: absolute; opacity: 0; }
  label span { display: grid; min-width: 0; }
  strong { color: var(--fg); font-size: 12px; }
  small { color: var(--fg-muted); font-size: 10px; font-weight: 400; }
  label:has(input:checked) strong { color: var(--accent-strong); }
  @media (max-width: 820px) {
    .choice-grid.three { grid-template-columns: 1fr; }
  }
</style>
