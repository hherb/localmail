<script lang="ts">
  import HtmlBody from "./HtmlBody.svelte";
  import AttachmentsStrip from "./AttachmentsStrip.svelte";
  import RawBodyView from "./RawBodyView.svelte";
  import HeaderUnfold from "./HeaderUnfold.svelte";
  import DebugChunks from "./DebugChunks.svelte";
  import { addressLabel, formatMessageDate } from "../lib/format";
  import { mail } from "../lib/stores/mail.svelte";
  import { settings } from "../lib/stores/settings.svelte";

  function setMode(m: "html" | "plain" | "raw") { mail.setBodyMode(m); }

  const externalImagesAllowed = $derived(
    settings.snapshot.imagePolicy === "allow" ||
      (settings.snapshot.imagePolicy === "ask" && mail.snapshot.externalImagesAllowed),
  );

  $effect(() => {
    if (
      settings.snapshot.imagePolicy === "allow" &&
      mail.snapshot.selectedMessage !== null &&
      !mail.snapshot.loadingDetail &&
      !mail.snapshot.externalImagesIncluded &&
      !mail.snapshot.loadingExternalImages
    ) {
      void mail.loadExternalImagesForSelectedMessage();
    }
  });
</script>

<article class="pane">
  {#if mail.snapshot.loadingDetail}
    <div class="hint">Loading…</div>
  {:else if mail.snapshot.selectedMessage}
    {@const m = mail.snapshot.selectedMessage}
    <header>
      <h2>{m.subject ?? "(no subject)"}</h2>
      <dl class="headers">
        <dt>From</dt><dd>{addressLabel(m.from)}</dd>
        {#if m.to.length}
          <dt>To</dt><dd>{m.to.map(addressLabel).join(", ")}</dd>
        {/if}
        {#if m.cc.length}
          <dt>Cc</dt><dd>{m.cc.map(addressLabel).join(", ")}</dd>
        {/if}
        <dt>Date</dt><dd>{formatMessageDate(m.date, settings.snapshot.dateFormat)}</dd>
        <dt>Account</dt>
        <dd>
          {m.account.name ?? m.account.id}
          {#if m.folders.length}
            <span class="folders"> · {m.folders.map((f) => f.name).join(", ")}</span>
          {/if}
        </dd>
      </dl>
      <div class="unfold"><HeaderUnfold messageId={String(m.id)} /></div>
    </header>

    <nav class="modes">
      <button
        type="button"
        class:active={mail.snapshot.bodyMode === "html"}
        onclick={() => setMode("html")}
        disabled={!m.body_html}
      >HTML</button>
      <button
        type="button"
        class:active={mail.snapshot.bodyMode === "plain"}
        onclick={() => setMode("plain")}
        disabled={!m.body_text}
      >Plain</button>
      <button
        type="button"
        class:active={mail.snapshot.bodyMode === "raw"}
        onclick={() => setMode("raw")}
      >Raw</button>

      {#if mail.snapshot.bodyMode === "html"
           && m.body_html
           && settings.snapshot.imagePolicy === "ask"
           && !mail.snapshot.externalImagesAllowed}
        <button
          type="button"
          class="images"
          onclick={() => void mail.loadExternalImagesForSelectedMessage()}
          disabled={mail.snapshot.loadingExternalImages}
        >
          {mail.snapshot.loadingExternalImages ? "Loading images…" : "Load images for this message"}
        </button>
      {:else if mail.snapshot.bodyMode === "html"
           && m.body_html
           && settings.snapshot.imagePolicy === "block"}
        <span class="image-status">Remote images blocked</span>
      {/if}
    </nav>

    <section class="body">
      {#if mail.snapshot.bodyMode === "html" && m.body_html}
        <HtmlBody
          html={m.body_html}
          allowExternalImages={externalImagesAllowed}
        />
      {:else if mail.snapshot.bodyMode === "plain" && m.body_text}
        <pre class="plain">{m.body_text}</pre>
      {:else if mail.snapshot.bodyMode === "raw"}
        <RawBodyView messageId={String(m.id)} />
      {:else if mail.snapshot.bodyMode === "html" && !m.body_html && m.body_text}
        <pre class="plain">{m.body_text}</pre>
      {:else}
        <p class="placeholder">No {mail.snapshot.bodyMode} body available.</p>
      {/if}
    </section>

    <AttachmentsStrip />

    {#if settings.snapshot.debug}
      <section class="debug" data-testid="debug-chunks-wrap">
        <DebugChunks matchedChunks={m.matched_chunks} />
      </section>
    {/if}
  {:else}
    <div class="hint">Select a message to read it.</div>
  {/if}
</article>

<style>
  .pane {
    height: 100%;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--surface);
  }
  .hint {
    margin: 48px auto;
    text-align: center;
    color: var(--fg-faint);
    font-size: 13px;
  }
  header {
    flex-shrink: 0;
    border-bottom: 1px solid var(--border);
    padding: 20px 24px 15px;
  }
  h2 {
    margin: 0 0 8px 0;
    font-size: 19px;
    line-height: 1.3;
    letter-spacing: -0.018em;
  }
  .headers {
    display: grid;
    grid-template-columns: auto 1fr;
    column-gap: 12px;
    row-gap: 2px;
    margin: 0;
    font-size: 12px;
  }
  .headers dt {
    color: var(--fg-faint);
  }
  .headers dd {
    margin: 0;
    color: var(--fg);
  }
  .folders {
    color: var(--fg-muted);
  }
  .modes {
    display: flex;
    gap: 4px;
    padding: 6px 12px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-subtle);
    flex-shrink: 0;
  }
  .modes button {
    min-height: 28px;
    padding: 3px 9px;
    font-size: 12px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    cursor: pointer;
  }
  .modes button.active {
    background: var(--surface);
    border-color: var(--border);
    color: var(--accent-strong);
    box-shadow: var(--shadow-sm);
  }
  .modes button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .modes button.images {
    margin-left: auto;
    border-color: #d3d3ed;
    background: var(--accent-soft);
    color: var(--accent-strong);
  }
  .image-status {
    margin-left: auto;
    align-self: center;
    color: var(--fg-faint);
    font-size: 10px;
  }
  .body {
    flex: 1;
    min-height: 0;
    overflow: auto;
    overscroll-behavior: contain;
  }
  .plain {
    white-space: pre-wrap;
    word-break: break-word;
    padding: 18px 22px;
    font: 13px/1.5 ui-monospace, SFMono-Regular, monospace;
    margin: 0;
    color: var(--fg);
  }
  .placeholder {
    padding: 16px;
    color: var(--fg-faint);
    font-style: italic;
  }
  .unfold {
    margin-top: 8px;
  }
  .debug {
    border-top: 1px dashed var(--border-strong);
    padding: 8px 12px;
    flex-shrink: 0;
    background: var(--surface-subtle);
  }
</style>
