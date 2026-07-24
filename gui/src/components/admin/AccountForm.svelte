<script lang="ts">
  /**
   * Create / edit one account. On edit the form loads the current row and
   * submits only the fields the operator actually changed — the server
   * writes every key it receives, so sending an untouched field would
   * rewrite it (and sending null would blank it).
   *
   * `archive` accounts have no IMAP endpoint, so the host/port inputs are
   * hidden for that auth method; `oauth2` accounts get their refresh token
   * from the web OAuth flow, not from this form.
   */
  import { onMount } from "svelte";

  import {
    createAdminAccount,
    getAdminAccount,
    updateAdminAccount,
    type AdminAccount,
    type AdminAccountInput,
    type AdminAccountPatch,
    type AdminAuthMethod,
  } from "../../lib/api/admin_accounts";
  import { hasImapEndpoint } from "../../lib/admin_auth_method";
  import { formatError } from "../../lib/format_error";

  interface Props {
    accountId: string | null;
    onSaved: () => void;
    onCancel: () => void;
  }
  let { accountId, onSaved, onCancel }: Props = $props();

  const DEFAULT_IMAP_PORT = 993;
  const OAUTH_PROVIDER_GMAIL = "gmail";

  let name: string = $state("");
  let emailAddress: string = $state("");
  let authMethod: AdminAuthMethod = $state("password");
  let imapHost: string = $state("");
  let imapPort: string = $state("");
  let loaded: AdminAccount | null = $state(null);
  let errorMessage: string | null = $state(null);
  let saving: boolean = $state(false);

  const isEdit = $derived(accountId !== null);
  const needsImap = $derived(hasImapEndpoint(authMethod));

  onMount(async () => {
    if (accountId === null) return;
    try {
      const acct = await getAdminAccount(accountId);
      loaded = acct;
      name = acct.name;
      emailAddress = acct.email_address;
      authMethod = acct.auth_method;
      imapHost = acct.imap_host ?? "";
      imapPort = acct.imap_port === null ? "" : String(acct.imap_port);
    } catch (err: unknown) {
      errorMessage = formatError(err);
    }
  });

  function parsePort(raw: string): number | undefined {
    const trimmed = raw.trim();
    if (trimmed === "") return undefined;
    const n = Number(trimmed);
    return Number.isInteger(n) ? n : undefined;
  }

  function buildCreateInput(): AdminAccountInput {
    const input: AdminAccountInput = {
      name: name.trim(),
      email_address: emailAddress.trim(),
      auth_method: authMethod,
    };
    if (needsImap) {
      const host = imapHost.trim();
      if (host !== "") input.imap_host = host;
      const port = parsePort(imapPort);
      if (port !== undefined) input.imap_port = port;
    }
    if (authMethod === "oauth2") input.oauth_provider = OAUTH_PROVIDER_GMAIL;
    return input;
  }

  // Only changed fields go into the patch. A cleared port cannot be sent:
  // omitting it leaves the column alone, and null would blank it.
  //
  // auth_method is deliberately not patched here — the selector is locked on
  // edit. Every transition dead-ends under the omit-unset PATCH design: → oauth2
  // needs an oauth_provider the web consent flow supplies, and → archive needs
  // imap_host/imap_port nulled, which omit-unset cannot express. Changing an
  // account's auth method means recreating it.
  function buildPatch(current: AdminAccount): AdminAccountPatch {
    const patch: AdminAccountPatch = {};
    if (emailAddress.trim() !== current.email_address) {
      patch.email_address = emailAddress.trim();
    }
    const host = imapHost.trim();
    if (host !== (current.imap_host ?? "")) patch.imap_host = host;
    const port = parsePort(imapPort);
    if (port !== (current.imap_port ?? undefined) && port !== undefined) {
      patch.imap_port = port;
    }
    return patch;
  }

  async function onSubmit(event: Event): Promise<void> {
    event.preventDefault();
    errorMessage = null;
    // A non-empty port that doesn't parse would otherwise be silently dropped
    // (omitted from the body); tell the operator instead of ignoring it.
    if (needsImap && imapPort.trim() !== "" && parsePort(imapPort) === undefined) {
      errorMessage = "IMAP port must be a whole number.";
      return;
    }
    saving = true;
    try {
      if (loaded !== null) {
        await updateAdminAccount(loaded.id, buildPatch(loaded));
      } else {
        await createAdminAccount(buildCreateInput());
      }
      onSaved();
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      saving = false;
    }
  }
</script>

<form data-testid="account-form" onsubmit={onSubmit}>
  <h3>{isEdit ? "Edit account" : "New account"}</h3>

  {#if errorMessage}
    <p class="error" data-testid="account-form-error" role="alert">{errorMessage}</p>
  {/if}

  <label>
    Name
    <input data-testid="field-name" bind:value={name} disabled={isEdit} required />
  </label>

  <label>
    Email address
    <input data-testid="field-email" bind:value={emailAddress} required />
  </label>

  <label>
    Auth method
    <select data-testid="field-auth-method" bind:value={authMethod} disabled={isEdit}>
      <option value="password">password</option>
      <option value="oauth2">oauth2 (Gmail)</option>
      <option value="archive">archive (no IMAP)</option>
    </select>
    {#if isEdit}
      <span class="hint" data-testid="auth-method-locked">
        Recreate the account to change its auth method.
      </span>
    {/if}
  </label>

  {#if needsImap}
    <label>
      IMAP host
      <input data-testid="field-imap-host" bind:value={imapHost} />
    </label>
    <label>
      IMAP port
      <input
        data-testid="field-imap-port"
        bind:value={imapPort}
        inputmode="numeric"
        placeholder={String(DEFAULT_IMAP_PORT)}
      />
    </label>
  {/if}

  <div class="actions">
    <button type="submit" data-testid="account-form-submit" disabled={saving}>
      {isEdit ? "Save" : "Create"}
    </button>
    <button type="button" data-testid="account-form-cancel" onclick={onCancel}>
      Cancel
    </button>
  </div>
</form>

<style>
  form {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    max-width: 30rem;
    font-size: 0.9rem;
  }
  h3 {
    margin: 0;
    font-size: 1rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
  }
  .error {
    color: #b3261e;
    margin: 0;
  }
  .hint {
    color: #777;
    font-size: 0.8rem;
  }
</style>
