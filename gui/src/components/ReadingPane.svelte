<script lang="ts">
  import HtmlBody from "./HtmlBody.svelte";
  import { addressLabel, formatRelativeDate } from "../lib/format";
  import { mail } from "../lib/stores/mail.svelte";

  function setMode(m: "html" | "plain" | "raw") { mail.setBodyMode(m); }
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
        <dt>Date</dt><dd>{formatRelativeDate(m.date)}</dd>
        <dt>Account</dt>
        <dd>
          {m.account.name ?? m.account.id}
          {#if m.folders.length}
            <span class="folders"> · {m.folders.map((f) => f.name).join(", ")}</span>
          {/if}
        </dd>
      </dl>
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
           && !mail.snapshot.externalImagesAllowed}
        <button type="button" class="images" onclick={() => mail.setExternalImagesAllowed(true)}>
          Load images for this message
        </button>
      {/if}
    </nav>

    <section class="body">
      {#if mail.snapshot.bodyMode === "html" && m.body_html}
        <HtmlBody
          html={m.body_html}
          allowExternalImages={mail.snapshot.externalImagesAllowed}
        />
      {:else if mail.snapshot.bodyMode === "plain" && m.body_text}
        <pre class="plain">{m.body_text}</pre>
      {:else if mail.snapshot.bodyMode === "raw"}
        <p class="placeholder">Raw RFC822 view arrives with the headers-unfold widget in Sub-plan 5.</p>
      {:else if mail.snapshot.bodyMode === "html" && !m.body_html && m.body_text}
        <pre class="plain">{m.body_text}</pre>
      {:else}
        <p class="placeholder">No {mail.snapshot.bodyMode} body available.</p>
      {/if}
    </section>
  {:else}
    <div class="hint">Select a message to read it.</div>
  {/if}
</article>

<style>
  .pane {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #fff;
  }
  .hint {
    margin: 48px auto;
    text-align: center;
    color: #888;
    font-size: 13px;
  }
  header {
    border-bottom: 1px solid #eee;
    padding: 16px 20px 12px;
  }
  h2 {
    margin: 0 0 8px 0;
    font-size: 18px;
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
    color: #888;
  }
  .headers dd {
    margin: 0;
    color: #222;
  }
  .folders {
    color: #888;
  }
  .modes {
    display: flex;
    gap: 4px;
    padding: 6px 12px;
    border-bottom: 1px solid #eee;
    background: #fafbfd;
    flex-shrink: 0;
  }
  .modes button {
    padding: 2px 8px;
    font-size: 12px;
    background: #fff;
    border: 1px solid #ccc;
    border-radius: 3px;
    cursor: pointer;
  }
  .modes button.active {
    background: #e1edff;
    border-color: #99b8e0;
  }
  .modes button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .modes button.images {
    margin-left: auto;
    color: #2a4d99;
  }
  .body {
    flex: 1;
    min-height: 0;
    overflow: auto;
  }
  .plain {
    white-space: pre-wrap;
    word-break: break-word;
    padding: 8px 12px;
    font: 13px/1.5 ui-monospace, SFMono-Regular, monospace;
    margin: 0;
    color: #222;
  }
  .placeholder {
    padding: 16px;
    color: #888;
    font-style: italic;
  }
</style>
