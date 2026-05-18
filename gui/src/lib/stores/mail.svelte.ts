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
 *   setBodyMode(mode)                                    switch html/plain/raw; sticky
 *   setExternalImagesAllowed(v)                          allow/block external images; resets per-message
 *   reset()                                              clear all state (used on logout)
 */
import { getChanges } from "../api/changes";
import { POLL_INTERVAL_MS, dedupNewMessages, parseCursor } from "../change_poller";
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
  bodyMode: "html" | "plain" | "raw";
  externalImagesAllowed: boolean;
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
    bodyMode: "html",
    externalImagesAllowed: false,
  };
}

class MailStore {
  #state: MailState = $state(initialState());
  #changeCursor: string | null = null;
  #pollHandle: ReturnType<typeof setInterval> | null = null;

  get snapshot(): MailState {
    return this.#state;
  }

  get changeCursor(): string | null {
    return this.#changeCursor;
  }

  get isPolling(): boolean {
    return this.#pollHandle !== null;
  }

  reset(): void {
    this.stopPolling();
    this.#changeCursor = null;
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
      this.#changeCursor = parseCursor(resp.next_cursor) ?? this.#changeCursor;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loadingMessages = false;
    }
  }

  mergeNewMessages(incoming: readonly MessageSummary[]): number {
    const fresh = dedupNewMessages(this.#state.messages, incoming);
    if (fresh.length > 0) {
      this.#state.messages = [...fresh, ...this.#state.messages];
    }
    return fresh.length;
  }

  async pollOnce(): Promise<void> {
    try {
      const resp = await getChanges(this.#changeCursor);
      this.#changeCursor = parseCursor(resp.next_cursor) ?? this.#changeCursor;
      this.mergeNewMessages(resp.new_messages);
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    }
  }

  startPolling(): void {
    if (this.#pollHandle !== null) return;
    this.#pollHandle = setInterval(() => {
      void this.pollOnce();
    }, POLL_INTERVAL_MS);
  }

  stopPolling(): void {
    if (this.#pollHandle !== null) {
      clearInterval(this.#pollHandle);
      this.#pollHandle = null;
    }
  }

  setSelection(sel: Selection): void {
    this.#state.selection = sel;
  }

  setBodyMode(mode: "html" | "plain" | "raw"): void {
    this.#state.bodyMode = mode;
  }

  setExternalImagesAllowed(v: boolean): void {
    this.#state.externalImagesAllowed = v;
  }

  async openMessage(messageId: string): Promise<void> {
    if (this.#state.selectedMessage?.id === messageId) return;
    this.#state.loadingDetail = true;
    this.#state.errorMessage = null;
    this.#state.externalImagesAllowed = false;
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
