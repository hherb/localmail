<script lang="ts">
  import { save } from "@tauri-apps/plugin-dialog";
  import { downloadAttachment } from "../lib/tauri";

  let { filename, sha256, onPreview = null } = $props<{
    filename: string | null;
    sha256: string | null;
    onPreview?: (() => void) | null;
  }>();

  let downloading = $state(false);
  let error: string | null = $state(null);

  function ext(): string {
    if (!filename) return "";
    const idx = filename.lastIndexOf(".");
    return idx >= 0 ? filename.slice(idx).toLowerCase() : "";
  }

  function canPreview(): boolean {
    const e = ext();
    return e === ".pdf" || e === ".png" || e === ".jpg" || e === ".jpeg" || e === ".gif" || e === ".webp";
  }

  async function download() {
    if (!sha256) return;
    downloading = true; error = null;
    try {
      const dest = await save({ defaultPath: filename ?? sha256 });
      if (!dest) { downloading = false; return; }
      await downloadAttachment(sha256, dest as string);
    } catch (e: unknown) {
      error = String(e);
    } finally {
      downloading = false;
    }
  }
</script>

<div class="attachment">
  <span class="name">{filename ?? "(unnamed)"}</span>
  {#if canPreview() && onPreview}
    <button type="button" onclick={onPreview} title="Preview">👁</button>
  {/if}
  <button type="button" onclick={download} disabled={downloading || !sha256} title="Download">
    {downloading ? "…" : "⤓"} Download
  </button>
  {#if error}
    <span class="error">{error}</span>
  {/if}
</div>

<style>
  .attachment { display: inline-flex; align-items: center; gap: 6px;
                background: #f4f6f9; border: 1px solid #ccd3df; padding: 4px 8px;
                border-radius: 6px; margin: 0 6px 6px 0; font-size: 12px; }
  .name { font-weight: 500; }
  button { background: #fff; border: 1px solid #bbb; padding: 1px 6px;
           border-radius: 3px; cursor: pointer; font-size: 12px; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .error { color: #c00; }
</style>
