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
import { formatError } from "../format_error";
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

// Hard ceiling on retained recent messages. /v1/changes prepends fresh items
// and a long-running session would grow this unboundedly otherwise. Picked
// to comfortably exceed any reasonable single-page render (settings.pageSize
// caps out around 200) while keeping memory + MessageList render bounded.
export const MAX_RECENT_MESSAGES = 1000;

// Consecutive pollOnce failures tolerated before the loop stops itself. After
// bearer-token expiry every poll fails identically; without this the UI would
// silently retry forever every POLL_INTERVAL_MS. Counter resets on success.
export const MAX_POLL_FAILURES = 5;

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
  #pollFailureCount: number = 0;

  get snapshot(): MailState {
    return this.#state;
  }

  get changeCursor(): string | null {
    return this.#changeCursor;
  }

  get isPolling(): boolean {
    return this.#pollHandle !== null;
  }

  get pollFailureCount(): number {
    return this.#pollFailureCount;
  }

  reset(): void {
    this.stopPolling();
    this.#changeCursor = null;
    this.#pollFailureCount = 0;
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
      this.#state.messages = resp.new_messages.slice(0, MAX_RECENT_MESSAGES);
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
      const merged = [...fresh, ...this.#state.messages];
      this.#state.messages =
        merged.length > MAX_RECENT_MESSAGES ? merged.slice(0, MAX_RECENT_MESSAGES) : merged;
    }
    return fresh.length;
  }

  async pollOnce(): Promise<void> {
    try {
      const resp = await getChanges(this.#changeCursor);
      this.#changeCursor = parseCursor(resp.next_cursor) ?? this.#changeCursor;
      this.mergeNewMessages(resp.new_messages);
      this.#pollFailureCount = 0;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
      this.#pollFailureCount += 1;
      if (this.#pollFailureCount >= MAX_POLL_FAILURES && this.#pollHandle !== null) {
        this.stopPolling();
        this.#state.errorMessage = `polling stopped after ${MAX_POLL_FAILURES} consecutive failures (last: ${formatError(err)})`;
      }
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

export const mail = new MailStore();
