<script lang="ts">
  /**
   * Right pane. Renders headers + plain-text body for the currently-open
   * message. HTML rendering and attachments land in Sub-plan 4.
   */
  import { addressLabel, formatRelativeDate } from "../lib/format";
  import { mail } from "../lib/stores/mail.svelte";
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

    {#if m.body_text}
      <pre class="body">{m.body_text}</pre>
    {:else}
      <div class="hint">No plain-text body. (HTML rendering arrives in Sub-plan 4.)</div>
    {/if}
  {:else}
    <div class="hint">Select a message to read it.</div>
  {/if}
</article>

<style>
  .pane {
    height: 100%;
    overflow-y: auto;
    padding: 16px 20px;
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
    padding-bottom: 12px;
    margin-bottom: 16px;
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
  .body {
    white-space: pre-wrap;
    word-break: break-word;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 13px;
    margin: 0;
    color: #222;
  }
</style>
