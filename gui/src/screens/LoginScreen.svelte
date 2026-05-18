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

<main class="container">
  <h1>Log in</h1>

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

    <button type="submit" disabled={pending || !username || !password}>
      {pending ? "Logging in…" : "Log in"}
    </button>
  </form>

  {#if errorMessage}
    <p class="error">{errorMessage}</p>
  {/if}

  <button class="link" onclick={onReconnect}>Connect to a different server</button>
</main>

<style>
  .container {
    max-width: 400px;
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
    margin-top: 4px;
  }
  button[type="submit"] {
    margin-top: 12px;
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
  .link {
    margin-top: 24px;
    padding: 4px 0;
    background: none;
    border: none;
    color: #1a4fc7;
    cursor: pointer;
    font-size: 12px;
    text-align: center;
    width: 100%;
  }
  .link:hover {
    text-decoration: underline;
    background: none;
  }
</style>
