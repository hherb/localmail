<script lang="ts">
  /**
   * Display tab: density, date format, and the default HTML-image policy.
   * Pure form controls — state lives in the settings store, which persists
   * to localStorage on every mutation.
   */
  import { settings } from "../../lib/stores/settings.svelte";
</script>

<section class="display">
  <h3>Density</h3>
  <label>
    <input
      type="radio"
      name="density"
      value="comfortable"
      checked={settings.snapshot.density === "comfortable"}
      onchange={() => settings.setDensity("comfortable")}
    />
    Comfortable
  </label>
  <label>
    <input
      type="radio"
      name="density"
      value="compact"
      checked={settings.snapshot.density === "compact"}
      onchange={() => settings.setDensity("compact")}
    />
    Compact
  </label>

  <h3>Date format</h3>
  <label>
    <input
      type="radio"
      name="dateFormat"
      value="relative"
      checked={settings.snapshot.dateFormat === "relative"}
      onchange={() => settings.setDateFormat("relative")}
    />
    Relative (yesterday)
  </label>
  <label>
    <input
      type="radio"
      name="dateFormat"
      value="absolute"
      checked={settings.snapshot.dateFormat === "absolute"}
      onchange={() => settings.setDateFormat("absolute")}
    />
    Absolute
  </label>

  <h3>HTML images</h3>
  <label>
    <input
      type="radio"
      name="img"
      value="block"
      checked={settings.snapshot.imagePolicy === "block"}
      onchange={() => settings.setImagePolicy("block")}
    />
    Block by default
  </label>
  <label>
    <input
      type="radio"
      name="img"
      value="ask"
      checked={settings.snapshot.imagePolicy === "ask"}
      onchange={() => settings.setImagePolicy("ask")}
    />
    Ask each time
  </label>
  <label>
    <input
      type="radio"
      name="img"
      value="allow"
      checked={settings.snapshot.imagePolicy === "allow"}
      onchange={() => settings.setImagePolicy("allow")}
    />
    Always allow
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
