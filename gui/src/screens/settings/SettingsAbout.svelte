<script lang="ts">
  /**
   * About tab: client build version, server-advertised API/build versions,
   * and a button that opens the platform-specific log directory via the
   * `open_logs_cmd` Tauri command. `__APP_VERSION__` is injected by
   * vite.config.ts from `gui/package.json`; it used to be a hand-kept literal
   * here and had drifted three minors ahead of it.
   */
  import { version } from "../../lib/stores/version.svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { buildLabel, versionWarning } from "../../lib/build_provenance";

  const CLIENT_VERSION = __APP_VERSION__;

  const serverFault = $derived(versionWarning(version.snapshot.info?.version_source));

  async function openLogs(): Promise<void> {
    try {
      await invoke("open_logs_cmd");
    } catch (e) {
      console.error(e);
    }
  }
</script>

<section class="about">
  <div class="section-heading">
    <h3>About localmail</h3>
    <p>A private, read-only window into your email archive.</p>
  </div>

  <div class="identity">
    <span class="brand-mark" aria-hidden="true"></span>
    <div><strong>localmail</strong><span>Desktop client {CLIENT_VERSION}</span></div>
  </div>

  <div class="setting-card">
    <div class="card-title">Version details</div>
    <dl>
      <dt>Client</dt>
      <dd>{CLIENT_VERSION}</dd>
      <dt>API version</dt>
      <dd>{version.snapshot.info?.api_major ?? "?"}.{version.snapshot.info?.api_minor ?? "?"}</dd>
      <dt>Server</dt>
      <dd>
        {version.snapshot.info?.server_version ?? "?"}
        {#if serverFault}
          <span class="fault">({serverFault})</span>
        {/if}
      </dd>
      <dt>Server build</dt>
      <dd class="mono">{buildLabel(version.snapshot.info?.build_hash, version.snapshot.info?.build_source)}</dd>
    </dl>
  </div>

  <div class="logs">
    <div><strong>Local diagnostics</strong><p>Logs stay on this device and are never uploaded.</p></div>
    <button type="button" onclick={openLogs}>Open log directory</button>
  </div>
</section>

<style>
  .about { display: grid; gap: 14px; }
  .section-heading h3 { margin: 0; font-size: 18px; letter-spacing: -0.02em; }
  .section-heading p, .logs p { margin: 4px 0 0; color: var(--fg-muted); font-size: 12px; }
  .identity { display: flex; align-items: center; gap: 13px; padding: 4px 2px; }
  .identity > div { display: grid; }
  .identity strong { font-size: 16px; }
  .identity span { color: var(--fg-muted); font-size: 11px; }
  .brand-mark {
    width: 44px;
    height: 44px;
    border-radius: 13px;
    background: linear-gradient(145deg, #7272e8, #4b4bc3);
    box-shadow: 0 8px 18px rgba(74, 74, 194, 0.22);
  }
  .setting-card, .logs {
    padding: 15px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-subtle);
  }
  .card-title { margin-bottom: 12px; font-size: 13px; font-weight: 650; }
  dl {
    display: grid;
    grid-template-columns: 110px minmax(0, 1fr);
    gap: 8px 16px;
    margin: 0;
  }
  dt { color: var(--fg-faint); font-size: 11px; }
  dd { margin: 0; overflow-wrap: anywhere; font-size: 12px; }
  .fault { color: var(--danger); }
  .mono { font-size: 10px; }
  .logs { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
  .logs strong { font-size: 13px; }
</style>
