<script lang="ts">
  /**
   * Server tab: shows current server URL + cert pin SHA-256 (cached from the
   * connect/probe flow), exposes change-password and log out, plus
   * real re-trust and change-server actions. Both return to the existing
   * connection flow so certificate verification remains in one place.
   */
  import { onDestroy } from "svelte";
  import { changePassword } from "../../lib/api/change_password";
  import { auth } from "../../lib/stores/auth.svelte";
  import { mail } from "../../lib/stores/mail.svelte";
  import { search } from "../../lib/stores/search.svelte";

  let oldPassword: string = $state("");
  let newPassword: string = $state("");
  let busy: boolean = $state(false);
  let message: string | null = $state(null);

  const usernameDisplay = $derived(
    auth.snapshot.phase === "logged_in" ? auth.snapshot.username : "(logged out)",
  );

  function clearPasswordFields(): void {
    oldPassword = "";
    newPassword = "";
  }

  function isWrongOldPassword(err: unknown): boolean {
    if (!err || typeof err !== "object") return false;
    const o = err as { kind?: string; detail?: unknown };
    if (o.kind !== "Http" || !o.detail || typeof o.detail !== "object") return false;
    const d = o.detail as { kind?: string; detail?: { status?: number } };
    return d.kind === "HttpStatus" && d.detail?.status === 401;
  }

  function formatChangePasswordError(err: unknown): string {
    if (isWrongOldPassword(err)) return "Current password is incorrect.";
    return err instanceof Error ? err.message : String(err);
  }

  async function onChange(): Promise<void> {
    busy = true;
    message = null;
    try {
      await changePassword(oldPassword, newPassword);
      message = "Password changed.";
      clearPasswordFields();
    } catch (err: unknown) {
      message = formatChangePasswordError(err);
      // On a 401 the typed current-password was wrong; clear it so the user
      // can retry without a second-keystroke leak risk. On network/5xx errors
      // keep both fields so the user doesn't have to retype on every blip.
      if (isWrongOldPassword(err)) oldPassword = "";
    } finally {
      busy = false;
    }
  }

  // Belt-and-braces: if the user closes the Settings overlay (component
  // unmounts) any cleartext in the form must not survive in memory.
  onDestroy(clearPasswordFields);

  async function onLogout(): Promise<void> {
    busy = true;
    try {
      mail.reset();
      search.reset();
      await auth.logout();
    } finally {
      busy = false;
    }
  }

  async function onRetrust(): Promise<void> {
    busy = true;
    message = null;
    try {
      await auth.retrustServer();
    } catch (err: unknown) {
      message = err instanceof Error ? err.message : String(err);
      busy = false;
    }
  }

  async function onChangeServer(): Promise<void> {
    busy = true;
    mail.reset();
    search.reset();
    await auth.changeServer();
  }
</script>

<section class="server">
  <div class="section-heading">
    <h3>Server & security</h3>
    <p>Your credentials and certificate pin stay in the operating system keyring.</p>
  </div>

  <div class="setting-card connection-card">
    <div class="card-title"><span class="status-dot"></span><strong>Connected endpoint</strong></div>
    <dl>
      <dt>Server URL</dt>
      <dd data-testid="server-url">{auth.serverUrl ?? "(not connected)"}</dd>
      <dt>Signed in as</dt>
      <dd data-testid="server-username">{usernameDisplay}</dd>
      <dt>Certificate pin</dt>
      <dd class="mono" data-testid="server-cert-pin">{auth.certPin ?? "(unknown)"}</dd>
    </dl>
    <div class="actions">
      <button type="button" data-testid="retrust-button" onclick={onRetrust} disabled={busy || !auth.serverUrl}>
        Verify certificate again
      </button>
      <button type="button" class="secondary" data-testid="change-server-button" onclick={onChangeServer} disabled={busy}>
        Change server…
      </button>
    </div>
    <p class="help">Changing server signs you out and returns to secure connection setup.</p>
  </div>

  <div class="setting-card">
    <div class="card-title"><strong>Change password</strong></div>
    <form onsubmit={(e) => { e.preventDefault(); void onChange(); }}>
      <label>
        Current password
        <input
          type="password"
          autocomplete="current-password"
          data-testid="old-password"
          bind:value={oldPassword}
          disabled={busy}
        />
      </label>
      <label>
        New password
        <input
          type="password"
          autocomplete="new-password"
          data-testid="new-password"
          bind:value={newPassword}
          disabled={busy}
        />
      </label>
      <button
        class="primary"
        type="submit"
        data-testid="change-password-submit"
        disabled={busy || oldPassword === "" || newPassword === ""}
      >
        Change password
      </button>
    </form>
    {#if message}<p class="msg" data-testid="change-password-message">{message}</p>{/if}
  </div>

  <div class="session-row">
    <div><strong>End this session</strong><p>Your server and certificate remain configured.</p></div>
    <button type="button" data-testid="logout-button" onclick={onLogout} disabled={busy}>
      Log out
    </button>
  </div>
</section>

<style>
  .server { display: grid; gap: 14px; }
  .section-heading h3 { margin: 0; font-size: 18px; letter-spacing: -0.02em; }
  .section-heading p, .help, .session-row p {
    margin: 4px 0 0;
    color: var(--fg-muted);
    font-size: 12px;
  }
  .setting-card, .session-row {
    padding: 15px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-subtle);
  }
  .card-title { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #43a477; box-shadow: 0 0 0 3px #e3f5eb; }
  dl {
    display: grid;
    grid-template-columns: 110px minmax(0, 1fr);
    gap: 7px 12px;
    margin: 13px 0;
    padding: 12px;
    border-radius: 9px;
    background: var(--surface);
  }
  dt { color: var(--fg-faint); font-size: 11px; }
  dd { min-width: 0; margin: 0; color: var(--fg); font-size: 12px; overflow-wrap: anywhere; }
  .mono {
    font-size: 10px;
    word-break: break-all;
  }
  form {
    display: grid;
    grid-template-columns: 1fr 1fr auto;
    align-items: end;
    gap: 9px;
    margin-top: 12px;
  }
  form label { display: grid; gap: 5px; color: var(--fg-muted); font-size: 11px; }
  form input { width: 100%; }
  .primary { border-color: var(--accent); background: var(--accent); color: white; }
  .primary:hover:not(:disabled) { border-color: var(--accent-hover); background: var(--accent-hover); }
  .actions { display: flex; gap: 8px; }
  .secondary { background: transparent; }
  .session-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
  .session-row strong { font-size: 13px; }
  .session-row button { color: var(--danger); border-color: #edcbd1; background: var(--danger-soft); }
  .help { font-size: 10px; }
  .msg {
    margin: 10px 0 0;
    padding: 8px 10px;
    border-radius: 7px;
    background: var(--accent-soft);
    font-size: 12px;
    color: var(--accent-strong);
  }
  @media (max-width: 900px) {
    form { grid-template-columns: 1fr 1fr; }
    form button { grid-column: 1 / -1; justify-self: start; }
  }
</style>
