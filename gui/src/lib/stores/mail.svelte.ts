/**
 * Single source of truth for the GUI's mail browsing state. Rune-backed
 * singleton; mirrors the pattern of `auth.svelte.ts`.
 *
 * State:
 *   accounts          AccountSummary[]                  loaded once after login
 *   folders           Map<accountId, FolderSummary[]>   loaded lazily on expansion
 *   messages          MessageSummary[]                  current page set (via /v1/messages)
 *   selection         Selection                         what the tree currently selects
 *   selectedMessage   MessageDetail | null              detail for the currently-open message
 *   loadingMessages   boolean                           true during initial list fetch
 *   loadingMore       boolean                           true during loadMoreMessages fetch
 *   loadingDetail     boolean                           true during detail fetch
 *   errorMessage      string | null                     last error surfaced from a load
 *
 * Actions:
 *   loadAccounts()                                       fetch /v1/accounts
 *   loadFoldersFor(accountId)                            fetch folders, idempotent
 *   loadInitialMessages(opts?)                           fetch /v1/messages (first page)
 *   loadMoreMessages()                                   fetch next page, append to messages
 *   setSelection(sel)                                    update the left-rail selection + refetch
 *   openMessage(id)                                      fetch + store detail; no-op if same id
 *   setBodyMode(mode)                                    switch html/plain/raw; sticky
 *   setExternalImagesAllowed(v)                          allow/block external images; resets per-message
 *   reset()                                              clear all state (used on logout)
 */
import { getChanges } from "../api/changes";
import { formatError } from "../format_error";
import { POLL_INTERVAL_MS, parseCursor } from "../change_poller";
import {
  getMessage,
  listAccounts,
  listFolders,
  listMessages,
  type AccountSummary,
  type FolderSummary,
  type MessageDetail,
  type MessageSummary,
  type Selection,
} from "../tauri";

// Soft cap for the pendingNewMessages buffer. /v1/changes pushes fresh
// items into this buffer (not directly into `messages`); the banner shows
// `pendingNewMessages.length`. Cap prevents an unattended tab from growing
// the buffer unboundedly while idle.
export const MAX_PENDING_NEW_MESSAGES = 500;

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
  loadingMore: boolean;
  loadingDetail: boolean;
  errorMessage: string | null;
  bodyMode: "html" | "plain" | "raw";
  externalImagesAllowed: boolean;
  pendingNewMessages: MessageSummary[];
}

function initialState(): MailState {
  return {
    accounts: [],
    folders: new Map(),
    messages: [],
    selection: { kind: "all" },
    selectedMessage: null,
    loadingMessages: false,
    loadingMore: false,
    loadingDetail: false,
    errorMessage: null,
    bodyMode: "html",
    externalImagesAllowed: false,
    pendingNewMessages: [],
  };
}

function selectionsEqual(a: Selection, b: Selection): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "all" && b.kind === "all") return true;
  if (a.kind === "account" && b.kind === "account") return a.accountId === b.accountId;
  if (a.kind === "folder" && b.kind === "folder") {
    return a.accountId === b.accountId && a.folderId === b.folderId;
  }
  return false;
}

function selectionToFilterOpts(sel: Selection): {
  accountIds: string[]; folderIds: string[];
} {
  if (sel.kind === "all") return { accountIds: [], folderIds: [] };
  if (sel.kind === "account") return { accountIds: [sel.accountId], folderIds: [] };
  return { accountIds: [sel.accountId], folderIds: [sel.folderId] };
}

class MailStore {
  #state: MailState = $state(initialState());
  #changeCursor: string | null = null;
  #pollHandle: ReturnType<typeof setInterval> | null = null;
  #pollFailureCount: number = 0;

  #messagesCursor: string | null = null;
  #messagesHasMore: boolean = false;
  #loadMoreInFlight: Promise<void> | null = null;
  // Filter opts the *current* page set was fetched with; loadMoreMessages
  // re-uses them so a paginated browse stays scoped to the same selection.
  #currentFilterOpts: { accountIds: string[]; folderIds: string[] } = {
    accountIds: [], folderIds: [],
  };

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

  get messagesCursor(): string | null { return this.#messagesCursor; }
  get messagesHasMore(): boolean { return this.#messagesHasMore; }

  reset(): void {
    this.stopPolling();
    this.#changeCursor = null;
    this.#messagesCursor = null;
    this.#messagesHasMore = false;
    this.#loadMoreInFlight = null;
    this.#currentFilterOpts = { accountIds: [], folderIds: [] };
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

  async loadInitialMessages(opts?: {
    accountIds?: string[]; folderIds?: string[];
  }): Promise<void> {
    this.#state.loadingMessages = true;
    this.#state.errorMessage = null;
    this.#state.messages = [];
    this.#messagesCursor = null;
    this.#messagesHasMore = false;
    this.#currentFilterOpts = {
      accountIds: opts?.accountIds ?? [],
      folderIds: opts?.folderIds ?? [],
    };
    try {
      const resp = await listMessages({
        account_ids: this.#currentFilterOpts.accountIds,
        folder_ids: this.#currentFilterOpts.folderIds,
        limit: 50,
        cursor: null,
      });
      this.#state.messages = resp.messages;
      this.#messagesCursor = resp.next_cursor;
      this.#messagesHasMore = resp.next_cursor !== null;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loadingMessages = false;
    }
  }

  async loadMoreMessages(): Promise<void> {
    if (!this.#messagesHasMore || this.#messagesCursor === null) return;
    if (this.#loadMoreInFlight) {
      // Coalesce concurrent calls onto a single in-flight request.
      return this.#loadMoreInFlight;
    }
    const cursor = this.#messagesCursor;
    this.#state.loadingMore = true;
    const promise = (async () => {
      try {
        const resp = await listMessages({
          account_ids: this.#currentFilterOpts.accountIds,
          folder_ids: this.#currentFilterOpts.folderIds,
          limit: 50,
          cursor,
        });
        this.#state.messages = [...this.#state.messages, ...resp.messages];
        this.#messagesCursor = resp.next_cursor;
        this.#messagesHasMore = resp.next_cursor !== null;
      } catch (err: unknown) {
        this.#state.errorMessage = formatError(err);
      } finally {
        this.#state.loadingMore = false;
        this.#loadMoreInFlight = null;
      }
    })();
    this.#loadMoreInFlight = promise;
    return promise;
  }

  /**
   * Internal: append fresh polled messages to the pending buffer.
   * Dedups against both `messages` and `pendingNewMessages`. Returns the
   * number of items appended to the buffer.
   */
  mergePendingNewMessages_internal(incoming: readonly MessageSummary[]): number {
    const seen = new Set<string>();
    for (const m of this.#state.messages) seen.add(m.message_id);
    for (const m of this.#state.pendingNewMessages) seen.add(m.message_id);
    const fresh = incoming.filter((m) => !seen.has(m.message_id));
    if (fresh.length === 0) return 0;
    const merged = [...fresh, ...this.#state.pendingNewMessages];
    this.#state.pendingNewMessages =
      merged.length > MAX_PENDING_NEW_MESSAGES
        ? merged.slice(0, MAX_PENDING_NEW_MESSAGES)
        : merged;
    return fresh.length;
  }

  /**
   * Move the pending buffer into the visible list, clearing the banner.
   */
  mergePendingNewMessages(): void {
    if (this.#state.pendingNewMessages.length === 0) return;
    this.#state.messages = [
      ...this.#state.pendingNewMessages, ...this.#state.messages,
    ];
    this.#state.pendingNewMessages = [];
  }

  async pollOnce(): Promise<void> {
    try {
      const resp = await getChanges(this.#changeCursor);
      this.#changeCursor = parseCursor(resp.next_cursor) ?? this.#changeCursor;
      this.mergePendingNewMessages_internal(resp.new_messages);
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
    if (selectionsEqual(this.#state.selection, sel)) return;
    this.#state.selection = sel;
    const opts = selectionToFilterOpts(sel);
    void this.loadInitialMessages(opts);
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
