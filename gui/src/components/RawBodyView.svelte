<script lang="ts">
  /**
   * Raw RFC822 view. Renders only when `messageId` is set. Initial state is
   * empty (we don't want to refetch every time the user switches body mode);
   * a "Load raw bytes" button does the fetch on demand. Body is decoded as
   * UTF-8 with replacement — RFC822 isn't strictly UTF-8 but the use case
   * is debug/diagnostic, not byte-exact.
   */
  import { getRawMessage } from "../lib/api/raw_message";

  interface Props { messageId: string; }
  let { messageId }: Props = $props();

  let text: string | null = $state(null);
  let loading: boolean = $state(false);
  let errorMessage: string | null = $state(null);

  async function load(): Promise<void> {
    loading = true;
    errorMessage = null;
    try {
      const bytes = await getRawMessage(messageId);
      text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    } catch (err: unknown) {
      errorMessage = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  async function copy(): Promise<void> {
    if (text === null) return;
    await navigator.clipboard.writeText(text);
  }
</script>

{#if text === null}
  <div class="empty">
    <button onclick={load} disabled={loading}>{loading ? "Loading…" : "Load raw bytes"}</button>
    {#if errorMessage}<div class="error">{errorMessage}</div>{/if}
  </div>
{:else}
  <div class="bar">
    <button onclick={copy}>Copy</button>
  </div>
  <pre class="raw">{text}</pre>
{/if}

<style>
  .raw { white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace; font-size: 12px; }
  .error { color: #b00020; margin-top: 0.5rem; }
</style>
