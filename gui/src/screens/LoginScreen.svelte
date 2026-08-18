<script lang="ts">
  import { auth } from "../lib/stores/auth.svelte";

  let username: string = $state("");
  let password: string = $state("");
  let pending: boolean = $state(false);

  let errorMessage: string | null = $derived(
    auth.snapshot.phase === "logged_out" && auth.snapshot.errorMessage
      ? auth.snapshot.errorMessage
      : null
  );

  async function onSubmit(): Promise<void> {
    pending = true;
    try {
      await auth.login(username, password);
    } finally {
      pending = false;
    }
  }

  function onReconnect(): void {
    auth.reset();
  }
</script>

<main class="auth-shell">
  <section class="card">
  <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>localmail</span></div>
  <div class="intro">
    <p class="eyebrow">Welcome back</p>
    <h1>Open your archive</h1>
    <p>Sign in to browse your private mail history.</p>
  </div>

  <form
    onsubmit={(e: Event) => {
      e.preventDefault();
      void onSubmit();
    }}
  >
    <label for="username">Username</label>
    <input id="username" bind:value={username} autocomplete="username" />

    <label for="password">Password</label>
    <input
      id="password"
      type="password"
      bind:value={password}
      autocomplete="current-password"
    />

    <button class="primary" type="submit" disabled={pending || !username || !password}>
      {pending ? "Logging in…" : "Log in"}
    </button>
  </form>

  {#if errorMessage}
    <p class="error">{errorMessage}</p>
  {/if}

  <button class="link" onclick={onReconnect}>Connect to a different server</button>
  <p class="privacy">Private by design · no telemetry · read-only archive</p>
  </section>
</main>

<style>
  .auth-shell { width: 100%; height: 100%; display: grid; place-items: center; padding: 32px;
    background: radial-gradient(circle at 20% 15%, #e5e5ff 0, transparent 32%),
                radial-gradient(circle at 85% 80%, #e1f3ed 0, transparent 30%), var(--canvas); }
  .card { width: min(430px, 92vw); padding: 28px; border: 1px solid rgba(255,255,255,.75);
    border-radius: 22px; background: rgba(255,255,255,.94); box-shadow: var(--shadow-lg); }
  .brand { display: flex; align-items: center; gap: 9px; margin-bottom: 30px; font-weight: 700; }
  .brand-mark { width: 28px; height: 28px; border-radius: 9px;
    background: linear-gradient(145deg,#7272e8,#4b4bc3); box-shadow: 0 5px 12px #c4c4ef; }
  .intro { margin-bottom: 22px; }
  .eyebrow { margin: 0 0 4px; color: var(--accent); font-size: 10px; font-weight: 700;
    letter-spacing: .11em; text-transform: uppercase; }
  h1 { margin: 0; font-size: 26px; line-height: 1.2; letter-spacing: -.035em; }
  .intro > p:last-child { margin: 8px 0 0; color: var(--fg-muted); font-size: 13px; }
  form { display: grid; gap: 8px; }
  label { margin-top: 3px; color: var(--fg-muted); font-size: 11px; font-weight: 600; }
  button[type="submit"] { margin-top: 10px; }
  .primary { border-color: var(--accent); background: var(--accent); color: white; }
  .primary:hover:not(:disabled) { border-color: var(--accent-hover); background: var(--accent-hover); }
  .error { margin-top: 14px; padding: 10px 12px; border-radius: 8px; background: var(--danger-soft);
    color: var(--danger); font-size: 11px; }
  .link {
    margin-top: 18px;
    padding: 6px 0;
    min-height: 28px;
    background: none;
    border: none;
    color: var(--accent-strong);
    cursor: pointer;
    font-size: 11px;
    text-align: center;
    width: 100%;
  }
  .link:hover {
    text-decoration: underline;
    background: none;
  }
  .privacy { margin: 20px 0 0; text-align: center; color: var(--fg-faint); font-size: 10px; }
</style>
