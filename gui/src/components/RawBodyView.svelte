<script lang="ts">
  /**
   * Raw RFC822 view. Renders only when `messageId` is set. Initial state is
   * empty (we don't want to refetch every time the user switches body mode);
   * a "Load raw bytes" button does the fetch on demand. Bytes are kept in
   * state so the user can flip the encoding dropdown live without re-fetching.
   *
   * Encoding default is "Auto" — sniff `Content-Type: charset=` from the
   * raw headers, fall back to UTF-8. Manual UTF-8 / Latin-1 / Windows-1252
   * / Shift_JIS overrides let the user un-mojibake bodies whose declared
   * charset is wrong or missing.
   */
  import { getRawMessage } from "../lib/api/raw_message";
  import {
    AUTO_CHARSET,
    SUPPORTED_CHARSETS,
    decodeWithLabel,
    parseCharsetFromHeaders,
    resolveCharset,
  } from "../lib/charset_helpers";

  interface Props { messageId: string; }
  let { messageId }: Props = $props();

  let raw: Uint8Array | null = $state(null);
  let loading: boolean = $state(false);
  let errorMessage: string | null = $state(null);
  let selectedCharset: string = $state(AUTO_CHARSET);

  // sniffedCharset is "did the message declare a charset?" — drives the
  // visibility of the AUTO hint.
  // effectiveCharset is the canonicalised + decoder-validated label actually
  // used to decode the bytes; safe to show verbatim in the hint.
  let sniffedCharset = $derived(raw === null ? null : parseCharsetFromHeaders(raw));
  let effectiveCharset = $derived(
    raw === null ? null : resolveCharset(sniffedCharset, selectedCharset),
  );
  let text = $derived(
    raw === null || effectiveCharset === null
      ? null
      : decodeWithLabel(raw, effectiveCharset),
  );

  async function load(): Promise<void> {
    loading = true;
    errorMessage = null;
    try {
      raw = await getRawMessage(messageId);
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

{#if raw === null}
  <div class="empty">
    <button onclick={load} disabled={loading}>{loading ? "Loading…" : "Load raw bytes"}</button>
    {#if errorMessage}<div class="error">{errorMessage}</div>{/if}
  </div>
{:else}
  <div class="bar">
    <label class="charset">
      Encoding:
      <select bind:value={selectedCharset} aria-label="Text encoding">
        {#each SUPPORTED_CHARSETS as option (option.value)}
          <option value={option.value}>{option.label}</option>
        {/each}
      </select>
    </label>
    {#if selectedCharset === AUTO_CHARSET && sniffedCharset !== null}
      <span class="hint" data-testid="charset-detected">(detected: {effectiveCharset})</span>
    {/if}
    <button onclick={copy}>Copy</button>
  </div>
  <pre class="raw">{text}</pre>
{/if}

<style>
  .bar { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }
  .charset { display: inline-flex; align-items: center; gap: 0.25rem; font-size: 12px; }
  .hint { color: #666; font-size: 11px; }
  .raw { white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace; font-size: 12px; }
  .error { color: #b00020; margin-top: 0.5rem; }
</style>
