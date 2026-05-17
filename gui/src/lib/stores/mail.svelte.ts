/**
 * Single source of truth for the GUI's mail browsing state. Rune-backed
 * singleton; mirrors the pattern of `auth.svelte.ts`.
 *
 * State:
 *   accounts          AccountSummary[]                  loaded once after login
 *   folders           Map<accountId, FolderSummary[]>   loaded lazily on expansion
 *   messages          MessageSummary[]                  most-recent 200 (via /v1/changes)
 *   selection         Selection                         what the tree currently selects
 *   selectedMessage   MessageDetail | null              detail for the currently-open message
 *   loadingMessages   boolean                           true during list fetch
 *   loadingDetail     boolean                           true during detail fetch
 *   errorMessage      string | null                     last error surfaced from a load
 *
 * Actions:
 *   loadAccounts()                                       fetch /v1/accounts
 *   loadFoldersFor(accountId)                            fetch folders, idempotent
 *   loadRecentMessages()                                 fetch /v1/changes
 *   setSelection(sel)                                    update the left-rail selection
 *   openMessage(id)                                      fetch + store detail; no-op if same id
 *   reset()                                              clear all state (used on logout)
 */
import {
  getMessage,
  listAccounts,
  listFolders,
  listRecentMessages,
  type AccountSummary,
  type FolderSummary,
  type MessageDetail,
  type MessageSummary,
  type Selection,
} from "../tauri";

export interface MailState {
  accounts: AccountSummary[];
  folders: Map<string, FolderSummary[]>;
  messages: MessageSummary[];
  selection: Selection;
  selectedMessage: MessageDetail | null;
  loadingMessages: boolean;
  loadingDetail: boolean;
  errorMessage: string | null;
}

function initialState(): MailState {
  return {
    accounts: [],
    folders: new Map(),
    messages: [],
    selection: { kind: "all" },
    selectedMessage: null,
    loadingMessages: false,
    loadingDetail: false,
    errorMessage: null,
  };
}

class MailStore {
  #state: MailState = $state(initialState());

  get snapshot(): MailState {
    return this.#state;
  }

  reset(): void {
    this.#state = initialState();
  }

  async loadAccounts(): Promise<void> {
    this.#state.errorMessage = null;
    try {
      const list = await listAccounts();
      this.#state.accounts = list;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    }
  }

  async loadFoldersFor(accountId: string): Promise<void> {
    if (this.#state.folders.has(accountId)) return;
    try {
      const list = await listFolders(accountId);
      const next = new Map(this.#state.folders);
      next.set(accountId, list);
      this.#state.folders = next;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    }
  }

  async loadRecentMessages(): Promise<void> {
    this.#state.loadingMessages = true;
    this.#state.errorMessage = null;
    try {
      const resp = await listRecentMessages();
      this.#state.messages = resp.new_messages;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loadingMessages = false;
    }
  }

  setSelection(sel: Selection): void {
    this.#state.selection = sel;
  }

  async openMessage(messageId: string): Promise<void> {
    if (this.#state.selectedMessage?.id === messageId) return;
    this.#state.loadingDetail = true;
    this.#state.errorMessage = null;
    try {
      const detail = await getMessage(messageId);
      this.#state.selectedMessage = detail;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loadingDetail = false;
    }
  }
}

function formatError(err: unknown): string {
  if (err && typeof err === "object") {
    const o = err as { kind?: string; detail?: unknown };
    if (o.kind && o.detail !== undefined) {
      const detailStr =
        typeof o.detail === "object" && o.detail !== null
          ? formatError(o.detail)
          : String(o.detail);
      return `${o.kind}: ${detailStr}`;
    }
    if (o.kind) return String(o.kind);
  }
  return String(err);
}

export const mail = new MailStore();
