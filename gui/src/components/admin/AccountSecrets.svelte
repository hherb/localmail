<script lang="ts">
  /**
   * Per-account credential + reachability controls.
   *
   * Password storage applies to `password` accounts only — an `oauth2`
   * account's refresh token comes from the web consent flow, and an
   * `archive` account has no IMAP endpoint at all (so neither control
   * applies to it).
   */
  import {
    storeAdminAccountPassword,
    testAdminAccountConnection,
    type AdminAuthMethod,
    type ProbedFolder,
  } from "../../lib/api/admin_accounts";
  import { hasImapEndpoint, usesStoredPassword } from "../../lib/admin_auth_method";
  import { formatError } from "../../lib/format_error";

  interface Props {
    accountId: string;
    authMethod: AdminAuthMethod;
  }
  let { accountId, authMethod }: Props = $props();

  let password: string = $state("");
  let status: string | null = $state(null);
  let errorMessage: string | null = $state(null);
  let folders: ProbedFolder[] | null = $state(null);
  let busy: boolean = $state(false);

  const canStorePassword = $derived(usesStoredPassword(authMethod));
  const canTestConnection = $derived(hasImapEndpoint(authMethod));

  async function onStorePassword(): Promise<void> {
    busy = true;
    status = null;
    errorMessage = null;
    try {
      await storeAdminAccountPassword(accountId, password);
      password = "";
      status = "Password stored.";
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      busy = false;
    }
  }

  async function onTestConnection(): Promise<void> {
    busy = true;
    status = null;
    errorMessage = null;
    folders = null;
    try {
      const result = await testAdminAccountConnection(accountId);
      folders = result.folders;
      status = `Connected. ${result.folders.length} folder(s) visible.`;
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      busy = false;
    }
  }
</script>

<div class="secrets">
  {#if status}
    <p class="ok" data-testid="secrets-status">{status}</p>
  {/if}
  {#if errorMessage}
    <p class="error" data-testid="secrets-error" role="alert">{errorMessage}</p>
  {/if}

  {#if canStorePassword}
    <div class="row">
      <input
        type="password"
        data-testid="secrets-password"
        bind:value={password}
        placeholder="IMAP password"
        autocomplete="off"
      />
      <button
        data-testid="secrets-store-password"
        onclick={onStorePassword}
        disabled={busy || password === ""}
      >Store password</button>
    </div>
  {/if}

  {#if canTestConnection}
    <div class="row">
      <button
        data-testid="secrets-test-connection"
        onclick={onTestConnection}
        disabled={busy}
      >Test connection</button>
    </div>
  {/if}

  {#if folders !== null}
    <ul data-testid="secrets-folders">
      {#each folders as folder (folder.name)}
        <li>{folder.name} <span class="flags">{folder.flags.join(" ")}</span></li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .secrets {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    font-size: 0.9rem;
  }
  .row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  ul {
    margin: 0;
    padding-left: 1.1rem;
    max-height: 12rem;
    overflow: auto;
  }
  .flags {
    color: #777;
  }
  .ok {
    color: #146c2e;
    margin: 0;
  }
  .error {
    color: #b3261e;
    margin: 0;
  }
</style>
