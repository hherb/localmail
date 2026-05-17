<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { fetchAttachmentBytes } from "../lib/tauri";

  let { sha256, filename, onClose } = $props<{
    sha256: string;
    filename: string | null;
    onClose: () => void;
  }>();

  let blobUrl: string | null = $state(null);
  let contentType: string | null = $state(null);
  let error: string | null = $state(null);
  let canvasEl: HTMLCanvasElement | null = $state(null);

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") onClose();
  }

  function ext(): string {
    if (!filename) return "";
    const i = filename.lastIndexOf(".");
    return i >= 0 ? filename.slice(i).toLowerCase() : "";
  }

  function isPdf(): boolean {
    return ext() === ".pdf" || contentType === "application/pdf";
  }

  onMount(async () => {
    document.addEventListener("keydown", onKey);
    try {
      const blob = await fetchAttachmentBytes(sha256);
      contentType = blob.content_type;
      const u8 = new Uint8Array(blob.bytes);
      blobUrl = URL.createObjectURL(new Blob([u8], { type: blob.content_type ?? "application/octet-stream" }));
      if (isPdf()) {
        // Lazy-import only when we actually have a PDF.
        const pdfjs = await import("pdfjs-dist");
        // Vite's ?url import gets a stable hashed asset URL for the worker.
        const PdfjsWorker = (await import("pdfjs-dist/build/pdf.worker.mjs?url")).default;
        (pdfjs as unknown as { GlobalWorkerOptions: { workerSrc: string } })
          .GlobalWorkerOptions.workerSrc = PdfjsWorker;
        const doc = await pdfjs.getDocument({ data: u8 }).promise;
        const page = await doc.getPage(1);
        const viewport = page.getViewport({ scale: 1.5 });
        if (canvasEl) {
          canvasEl.width = viewport.width;
          canvasEl.height = viewport.height;
          const ctx = canvasEl.getContext("2d");
          if (ctx) {
            await page.render({ canvasContext: ctx, viewport }).promise;
          }
        }
      }
    } catch (e: unknown) {
      error = String(e);
    }
  });

  onDestroy(() => {
    document.removeEventListener("keydown", onKey);
    if (blobUrl) URL.revokeObjectURL(blobUrl);
  });
</script>

<div class="backdrop" onclick={onClose} onkeydown={onKey} role="button" tabindex="-1">
  <div class="modal" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="dialog" aria-label="Attachment preview" tabindex="-1">
    <header>
      <span>{filename ?? sha256}</span>
      <button type="button" onclick={onClose} aria-label="Close">×</button>
    </header>
    <section class="body">
      {#if error}
        <p class="error">{error}</p>
      {:else if !blobUrl}
        <p class="placeholder">Loading…</p>
      {:else if isPdf()}
        <canvas bind:this={canvasEl}></canvas>
      {:else}
        <img src={blobUrl} alt={filename ?? ""} />
      {/if}
    </section>
  </div>
</div>

<style>
  .backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.5);
              display: flex; align-items: center; justify-content: center;
              z-index: 1000; }
  .modal { background: #fff; border-radius: 6px; max-width: 80vw; max-height: 80vh;
           display: flex; flex-direction: column; box-shadow: 0 4px 16px rgba(0,0,0,0.2); }
  header { display: flex; justify-content: space-between; align-items: center;
           padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 13px; }
  header button { background: transparent; border: none; cursor: pointer;
                  font-size: 20px; color: #555; }
  .body { padding: 12px; overflow: auto; }
  .body img { max-width: 70vw; max-height: 70vh; display: block; }
  .body canvas { display: block; }
  .placeholder { color: #888; font-style: italic; }
  .error { color: #c00; }
</style>
