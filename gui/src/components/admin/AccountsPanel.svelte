<script lang="ts">
  /**
   * Admin → Accounts. Lists every configured account and offers the
   * per-row operations the JSON admin API exposes: pause/resume sync and
   * delete. A delete the server refuses because messages reference the
   * account (409) escalates to an explicit force confirmation rather than
   * a silent no-op.
   */
  import { onMount } from "svelte";

  import {
    deleteAdminAccount,
    listAdminAccounts,
    updateAdminAccount,
    type AdminAccountSummary,
  } from "../../lib/api/admin_accounts";
  import { isConflict } from "../../lib/admin_error";
  import { formatError } from "../../lib/format_error";
  import AccountForm from "./AccountForm.svelte";

  let rows: AdminAccountSummary[] = $state([]);
  let loading: boolean = $state(true);
  let errorMessage: string | null = $state(null);
  let busyId: string | null = $state(null);
  let forceDeleteId: string | null = $state(null);
  let forceDeleteReason: string | null = $state(null);
  let formOpen: boolean = $state(false);
  let editingId: string | null = $state(null);

  onMount(load);

  function openForm(id: string | null): void {
    editingId = id;
    formOpen = true;
  }

  async function onFormSaved(): Promise<void> {
    formOpen = false;
    await load();
  }

  async function load(): Promise<void> {
    loading = true;
    errorMessage = null;
    try {
      rows = await listAdminAccounts();
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      loading = false;
    }
  }

  async function onToggleSync(row: AdminAccountSummary): Promise<void> {
    busyId = row.id;
    errorMessage = null;
    try {
      await updateAdminAccount(row.id, { sync_enabled: !row.sync_enabled });
      await load();
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      busyId = null;
    }
  }

  async function onDelete(row: AdminAccountSummary, force: boolean): Promise<void> {
    busyId = row.id;
    errorMessage = null;
    try {
      await deleteAdminAccount(row.id, force);
      forceDeleteId = null;
      forceDeleteReason = null;
      await load();
    } catch (err: unknown) {
      if (!force && isConflict(err)) {
        forceDeleteId = row.id;
        forceDeleteReason = formatError(err);
      } else {
        forceDeleteId = null;
        errorMessage = formatError(err);
      }
    } finally {
      busyId = null;
    }
  }
</script>

<div class="panel">
  <div class="toolbar">
    <button data-testid="new-account" onclick={() => openForm(null)}>New account</button>
    <button data-testid="accounts-refresh" onclick={load} disabled={loading}>Refresh</button>
  </div>

  {#if errorMessage}
    <p class="error" data-testid="accounts-error" role="alert">{errorMessage}</p>
  {/if}

  {#if formOpen}
    <AccountForm
      accountId={editingId}
      onSaved={onFormSaved}
      onCancel={() => (formOpen = false)}
    />
  {/if}

  {#if loading}
    <p data-testid="accounts-loading">Loading accounts…</p>
  {:else if rows.length === 0}
    <p data-testid="accounts-empty">No accounts configured yet.</p>
  {:else}
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Address</th>
          <th>Auth</th>
          <th>Sync</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.id)}
          <tr data-testid="account-row-{row.id}">
            <td>{row.name}</td>
            <td>{row.email_address}</td>
            <td>{row.auth_method}</td>
            <td>
              <button
                data-testid="toggle-sync-{row.id}"
                onclick={() => onToggleSync(row)}
                disabled={busyId === row.id}
              >{row.sync_enabled ? "Pause" : "Resume"}</button>
            </td>
            <td>
              <button
                data-testid="edit-account-{row.id}"
                onclick={() => openForm(row.id)}
                disabled={busyId === row.id}
              >Edit</button>
              <button
                class="danger"
                data-testid="delete-account-{row.id}"
                onclick={() => onDelete(row, false)}
                disabled={busyId === row.id}
              >Delete</button>
            </td>
          </tr>
          {#if forceDeleteId === row.id}
            <tr class="confirm-row">
              <td colspan="5">
                <span class="confirm-text">{forceDeleteReason}</span>
                <button
                  class="danger"
                  data-testid="confirm-force-delete-{row.id}"
                  onclick={() => onDelete(row, true)}
                  disabled={busyId === row.id}
                >Delete anyway (removes its messages)</button>
                <button onclick={() => (forceDeleteId = null)}>Cancel</button>
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    font-size: 0.9rem;
  }
  .toolbar {
    display: flex;
    gap: 0.5rem;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    padding: 0.35rem 0.5rem;
    border-bottom: 1px solid #eee;
  }
  th {
    font-weight: 600;
    color: #555;
  }
  .danger {
    color: #b3261e;
  }
  .confirm-row td {
    background: #fff4f3;
  }
  .confirm-text {
    margin-right: 0.75rem;
  }
  .error {
    color: #b3261e;
    margin: 0;
  }
</style>
