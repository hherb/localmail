<script lang="ts">
  /**
   * About tab: client build version, server-advertised API/build versions,
   * and a button that opens the platform-specific log directory via the
   * `open_logs_cmd` Tauri command. `CLIENT_VERSION` is hand-kept in sync
   * with `gui/package.json` and `gui/src-tauri/Cargo.toml`; build-time
   * injection is out of scope for v1.
   */
  import { version } from "../../lib/stores/version.svelte";
  import { invoke } from "@tauri-apps/api/core";

  const CLIENT_VERSION = "0.5.0";

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
    <dd>{version.snapshot.info?.server_version ?? "?"}</dd>
    <dt>Server build</dt>
    <dd>{version.snapshot.info?.build_hash ?? "?"}</dd>
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
</style>
