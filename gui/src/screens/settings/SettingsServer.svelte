<script lang="ts">
  /**
   * Server tab: shows current server URL + cert pin SHA-256 (cached from the
   * connect/probe flow), exposes change-password, log out, and a
   * non-destructive "re-trust" action that just re-displays the current pin.
   * The actual TOFU prompt only happens on a fresh /connect flow.
   */
  import { changePassword } from "../../lib/api/change_password";
  import { auth } from "../../lib/stores/auth.svelte";

  let oldPassword: string = $state("");
  let newPassword: string = $state("");
  let busy: boolean = $state(false);
  let message: string | null = $state(null);
  let showPin: boolean = $state(false);

  const usernameDisplay = $derived(
    auth.snapshot.phase === "logged_in" ? auth.snapshot.username : "(logged out)",
  );

  async function onChange(): Promise<void> {
    busy = true;
    message = null;
    try {
      await changePassword(oldPassword, newPassword);
      message = "Password changed.";
      oldPassword = "";
      newPassword = "";
    } catch (err: unknown) {
      message = err instanceof Error ? err.message : String(err);
    } finally {
      busy = false;
    }
  }

  async function onLogout(): Promise<void> {
    busy = true;
    try {
      await auth.logout();
    } finally {
      busy = false;
    }
  }

  function onRetrust(): void {
    showPin = true;
  }
</script>

<section class="server">
  <h3>Server</h3>
  <dl>
    <dt>URL</dt>
    <dd data-testid="server-url">{auth.serverUrl ?? "(not connected)"}</dd>
    <dt>Username</dt>
    <dd data-testid="server-username">{usernameDisplay}</dd>
    <dt>Cert pin (SHA-256)</dt>
    <dd class="mono" data-testid="server-cert-pin">{auth.certPin ?? "(unknown)"}</dd>
  </dl>

  <h3>Change password</h3>
  <form onsubmit={(e) => { e.preventDefault(); void onChange(); }}>
    <label>
      Current
      <input
        type="password"
        data-testid="old-password"
        bind:value={oldPassword}
        disabled={busy}
      />
    </label>
    <label>
      New
      <input
        type="password"
        data-testid="new-password"
        bind:value={newPassword}
        disabled={busy}
      />
    </label>
    <button
      type="submit"
      data-testid="change-password-submit"
      disabled={busy || oldPassword === "" || newPassword === ""}
    >
      Change password
    </button>
  </form>
  {#if message}<p class="msg" data-testid="change-password-message">{message}</p>{/if}

  <h3>Trust</h3>
  <button type="button" data-testid="retrust-button" onclick={onRetrust} disabled={busy}>
    Re-trust cert
  </button>
  {#if showPin}
    <p class="msg" data-testid="retrust-message">
      Current pin: <span class="mono">{auth.certPin ?? "(unknown)"}</span>. To
      re-pin, disconnect and run the connect flow again.
    </p>
  {/if}

  <h3>Session</h3>
  <button type="button" data-testid="logout-button" onclick={onLogout} disabled={busy}>
    Log out
  </button>
</section>

<style>
  .mono {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    word-break: break-all;
  }
  form {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    max-width: 320px;
  }
  .msg {
    font-size: 14px;
    color: var(--fg-muted, #555);
  }
</style>
