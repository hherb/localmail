<script lang="ts">
  /**
   * Lazy-loads the full RFC822 headers via /v1/messages/{id}?headers=full.
   * Caches the result in component-local state so toggling Hide/Show doesn't
   * re-fetch.
   */
  import { getMessageFullHeaders } from "../lib/api/full_headers";

  type RawHeaders = Record<string, string | string[]>;

  interface Props { messageId: string; }
  let { messageId }: Props = $props();

  let headers: RawHeaders | null = $state(null);
  let visible: boolean = $state(false);
  let loading: boolean = $state(false);
  let errorMessage: string | null = $state(null);

  async function toggle(): Promise<void> {
    if (visible) {
      visible = false;
      return;
    }
    if (headers !== null) {
      visible = true;
      return;
    }
    loading = true;
    errorMessage = null;
    try {
      const resp = await getMessageFullHeaders(messageId);
      headers = (resp.headers ?? {}) as RawHeaders;
      visible = true;
    } catch (err: unknown) {
      errorMessage = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  function entries(h: RawHeaders): Array<[string, string]> {
    const out: Array<[string, string]> = [];
    for (const [k, v] of Object.entries(h)) {
      if (Array.isArray(v)) for (const one of v) out.push([k, one]);
      else out.push([k, v]);
    }
    return out;
  }
</script>

<button onclick={toggle} disabled={loading}>
  {#if loading}Loading…{:else if visible}Hide full headers{:else}Show full headers{/if}
</button>
{#if errorMessage}<div class="error">{errorMessage}</div>{/if}
{#if visible && headers}
  <dl class="hdrs">
    {#each entries(headers) as [name, value]}
      <dt>{name}</dt><dd>{value}</dd>
    {/each}
  </dl>
{/if}

<style>
  .hdrs { font-family: ui-monospace, monospace; font-size: 12px; margin-top: 0.5rem; }
  .hdrs dt { font-weight: bold; }
  .hdrs dd { margin: 0 0 0.25rem 0; word-break: break-all; }
  .error { color: #b00020; }
</style>
