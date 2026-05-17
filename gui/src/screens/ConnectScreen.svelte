<script lang="ts">
  import { auth } from "../lib/stores/auth.svelte";

  let url: string = $state("https://localhost:8443");
  let probing: boolean = $state(false);
  let error: string | null = $state(null);

  async function onProbe(): Promise<void> {
    error = null;
    probing = true;
    try {
      await auth.probe(url);
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      probing = false;
    }
  }

  async function onTrust(): Promise<void> {
    error = null;
    try {
      await auth.confirmTrust();
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  function onBack(): void {
    error = null;
    auth.reset();
  }
</script>

<main class="container">
  <h1>Connect to localmail server</h1>

  {#if auth.snapshot.phase === "connecting"}
    <form
      onsubmit={(e: Event) => {
        e.preventDefault();
        void onProbe();
      }}
    >
      <label for="server-url">Server URL</label>
      <input
        id="server-url"
        bind:value={url}
        placeholder="https://your-server:8443"
        autocomplete="off"
        spellcheck={false}
      />
      <button type="submit" disabled={probing}>
        {probing ? "Probing…" : "Connect"}
      </button>
    </form>
  {:else if auth.snapshot.phase === "needs_trust"}
    {@const snap = auth.snapshot}
    <div class="trust">
      <p>Server responded: <code>localmail {snap.serverVersion}</code>
         (API v{snap.apiMajor}.{snap.apiMinor})</p>
      <p>TLS certificate fingerprint (SHA-256):</p>
      <pre class="fp">{snap.certSha256}</pre>
      <p class="warn">
        Verify this fingerprint matches your server's certificate. If you trust this
        fingerprint, this client will pin it — any future certificate change will
        require re-trust.
      </p>
      <div class="row">
        <button onclick={onTrust}>Trust this certificate</button>
        <button onclick={onBack} class="secondary">Back</button>
      </div>
    </div>
  {/if}

  {#if error}
    <p class="error">{error}</p>
  {/if}
</main>

<style>
  .container {
    max-width: 640px;
    margin: 64px auto;
    padding: 24px;
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  }
  form {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  label {
    font-size: 12px;
    color: #555;
  }
  .trust {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .fp {
    margin: 0;
    padding: 12px;
    background: #f4f4f4;
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
    word-break: break-all;
    line-height: 1.4;
  }
  .warn {
    margin: 0;
    padding: 10px 12px;
    background: #fff8dc;
    border-left: 3px solid #d4a017;
    color: #5a4500;
    font-size: 12px;
  }
  .row {
    display: flex;
    gap: 8px;
  }
  .secondary {
    background: #f4f4f4;
    color: #555;
    border-color: #ccc;
  }
  .error {
    margin-top: 16px;
    padding: 12px;
    background: #fdecea;
    border-left: 3px solid #c0392b;
    color: #c0392b;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 12px;
  }
</style>
