<script lang="ts">
  import AttachmentRow from "./AttachmentRow.svelte";
  import { mail } from "../lib/stores/mail.svelte";

  let previewSha: string | null = $state(null);
  let previewFilename: string | null = $state(null);

  function openPreview(sha256: string | null, filename: string | null) {
    previewSha = sha256; previewFilename = filename;
  }
  function closePreview() {
    previewSha = null; previewFilename = null;
  }

  let atts = $derived(mail.snapshot.selectedMessage?.attachments ?? []);
</script>

{#if atts.length > 0}
  <div class="strip">
    {#each atts as a (a.sha256 ?? a.filename ?? Math.random())}
      <AttachmentRow
        filename={a.filename}
        sha256={a.sha256}
        onPreview={a.sha256 ? () => openPreview(a.sha256, a.filename) : null}
      />
    {/each}
  </div>
{/if}

{#if previewSha}
  {#await import("./AttachmentPreviewModal.svelte") then mod}
    {@const C = mod.default}
    <C sha256={previewSha} filename={previewFilename} onClose={closePreview} />
  {/await}
{/if}

<style>
  .strip { padding: 6px 12px; border-top: 1px solid #eee; background: #fafbfd;
           display: flex; flex-wrap: wrap; }
</style>
