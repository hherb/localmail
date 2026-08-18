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

<main class="auth-shell">
  <section class="card">
  <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>localmail</span></div>

  {#if auth.snapshot.phase === "connecting"}
    <div class="intro">
      <p class="eyebrow">Secure setup</p>
      <h1>Connect your archive</h1>
      <p>Enter the HTTPS address of the localmail server you want to browse.</p>
    </div>
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
      <button class="primary" type="submit" disabled={probing || !url.trim()}>
        {probing ? "Probing…" : "Connect"}
      </button>
    </form>
  {:else if auth.snapshot.phase === "needs_trust"}
    {@const snap = auth.snapshot}
    <div class="trust">
      <div class="intro">
        <p class="eyebrow">Identity check</p>
        <h1>Verify this server</h1>
        <p>Server responded as localmail {snap.serverVersion} · API {snap.apiMajor}.{snap.apiMinor}</p>
      </div>
      <p class="field-label">TLS certificate fingerprint · SHA-256</p>
      <pre class="fp">{snap.certSha256}</pre>
      <p class="warn">
        Compare this fingerprint with the server before continuing. localmail pins it
        on this device and warns you if the certificate changes.
      </p>
      <div class="row">
        <button class="primary" onclick={onTrust}>Trust and continue</button>
        <button onclick={onBack} class="secondary">Back</button>
      </div>
    </div>
  {/if}

  {#if error}
    <p class="error">{error}</p>
  {/if}
  <p class="privacy">Private by design · no telemetry · read-only archive</p>
  </section>
</main>

<style>
  .auth-shell {
    width: 100%; height: 100%; display: grid; place-items: center; padding: 32px;
    background: radial-gradient(circle at 20% 15%, #e5e5ff 0, transparent 32%),
                radial-gradient(circle at 85% 80%, #e1f3ed 0, transparent 30%), var(--canvas);
  }
  .card {
    width: min(500px, 92vw); padding: 28px; border: 1px solid rgba(255,255,255,.75);
    border-radius: 22px; background: rgba(255,255,255,.94); box-shadow: var(--shadow-lg);
  }
  .brand { display: flex; align-items: center; gap: 9px; margin-bottom: 30px; font-weight: 700; }
  .brand-mark { width: 28px; height: 28px; border-radius: 9px;
                background: linear-gradient(145deg,#7272e8,#4b4bc3); box-shadow: 0 5px 12px #c4c4ef; }
  .intro { margin-bottom: 22px; }
  .eyebrow { margin: 0 0 4px; color: var(--accent); font-size: 10px; font-weight: 700;
             letter-spacing: .11em; text-transform: uppercase; }
  h1 { margin: 0; font-size: 26px; line-height: 1.2; letter-spacing: -.035em; }
  .intro > p:last-child { margin: 8px 0 0; color: var(--fg-muted); font-size: 13px; }
  form { display: grid; gap: 8px; }
  form button { margin-top: 8px; }
  label, .field-label { font-size: 11px; color: var(--fg-muted); font-weight: 600; }
  .field-label { margin: 0; }
  .primary { border-color: var(--accent); background: var(--accent); color: white; }
  .primary:hover:not(:disabled) { border-color: var(--accent-hover); background: var(--accent-hover); }
  .trust { display: flex; flex-direction: column; gap: 12px; }
  .fp { margin: 0; padding: 13px; border: 1px solid var(--border); background: var(--surface-subtle);
        border-radius: 8px; font-size: 11px; word-break: break-all; white-space: pre-wrap; }
  .warn { margin: 0; padding: 11px 12px; border: 1px solid #f1dfb8; border-radius: 8px;
          background: var(--warning-soft); color: #72501d; font-size: 11px; }
  .row { display: flex; gap: 8px; margin-top: 3px; }
  .secondary { background: transparent; color: var(--fg-muted); }
  .error { margin-top: 14px; padding: 10px 12px; border-radius: 8px; background: var(--danger-soft);
           color: var(--danger); font-size: 11px; }
  .privacy { margin: 26px 0 0; text-align: center; color: var(--fg-faint); font-size: 10px; }
  @media (max-height: 620px) {
    .card { padding: 22px; }
    .brand { margin-bottom: 18px; }
  }
</style>
