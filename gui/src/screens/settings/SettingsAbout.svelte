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
  <h3>Versions</h3>
  <dl>
    <dt>Client</dt>
    <dd>{CLIENT_VERSION}</dd>
    <dt>API major</dt>
    <dd>{version.snapshot.info?.api_major ?? "?"}</dd>
    <dt>API minor</dt>
    <dd>{version.snapshot.info?.api_minor ?? "?"}</dd>
    <dt>Server</dt>
    <dd>
      {version.snapshot.info?.server_version ?? "?"}
      {#if serverFault}
        <span class="fault">({serverFault})</span>
      {/if}
    </dd>
    <dt>Server build</dt>
    <dd>{buildLabel(version.snapshot.info?.build_hash, version.snapshot.info?.build_source)}</dd>
  </dl>

  <h3>Logs</h3>
  <button type="button" onclick={openLogs}>Open log directory</button>
</section>

<style>
  dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.25rem 1rem;
    margin: 0.5rem 0;
  }
  dt {
    font-weight: 600;
  }
  dd {
    margin: 0;
  }
  h3 {
    margin-top: 1rem;
  }
  .fault {
    color: #c0392b;
  }
</style>
