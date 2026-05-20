import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  listMessages: vi.fn(),
  getMessage: vi.fn(),
  invoke: vi.fn(),
}));

vi.mock("../tauri", () => mocks);
vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));

import { mail, MAX_POLL_FAILURES, MAX_PENDING_NEW_MESSAGES } from "./mail.svelte";
import { POLL_INTERVAL_MS } from "../change_poller";
import type { AccountSummary, FolderSummary, MessageDetail, MessageSummary } from "../tauri";

const acct = (id: string, name: string): AccountSummary => ({
  id,
  name,
  address: `${name}@example.com`,
  last_sync_at: null,
  message_count: 0,
  capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
});

const folder = (id: string, name: string): FolderSummary => ({
  id,
  name,
  full_path: name,
  flags: null,
  last_uid: null,
  message_count: 0,
});

const msg = (id: string, accountId: string, subject = "hi"): MessageSummary => ({
  message_id: id,
  subject,
  from: { name: null, address: "x@example.com" },
  date: null,
  account: { id: accountId, name: null },
});

const detail = (id: string, body: string): MessageDetail => ({
  id,
  subject: "s",
  from: { name: null, address: null },
  to: [],
  cc: [],
  bcc: [],
  date: null,
  body_text: body,
  body_html: null,
  attachments: [],
  account: { id: "1", name: null, address: null },
  folders: [],
});

beforeEach(() => {
  mail.reset();
  vi.clearAllMocks();
});

describe("mail store", () => {
  it("starts empty with selection=all and no loaded data", () => {
    expect(mail.snapshot.accounts).toEqual([]);
    expect(mail.snapshot.messages).toEqual([]);
    expect(mail.snapshot.selection).toEqual({ kind: "all" });
    expect(mail.snapshot.selectedMessage).toBeNull();
    expect(mail.snapshot.loadingMessages).toBe(false);
  });

  it("loads accounts via listAccounts()", async () => {
    mocks.listAccounts.mockResolvedValue([acct("1", "alice"), acct("2", "bob")]);
    await mail.loadAccounts();
    expect(mail.snapshot.accounts).toHaveLength(2);
    expect(mail.snapshot.accounts[0].name).toBe("alice");
  });

  it("loads folders into per-account map", async () => {
    mocks.listFolders.mockResolvedValue([folder("10", "INBOX"), folder("11", "Sent")]);
    await mail.loadFoldersFor("1");
    expect(mail.snapshot.folders.get("1")).toHaveLength(2);
  });

  it("loadFoldersFor is idempotent — does not re-fetch if already loaded", async () => {
    mocks.listFolders.mockResolvedValue([folder("10", "INBOX")]);
    await mail.loadFoldersFor("1");
    await mail.loadFoldersFor("1");
    expect(mocks.listFolders).toHaveBeenCalledTimes(1);
  });

  it("loads messages and exposes them via snapshot.messages", async () => {
    mocks.listMessages.mockResolvedValue({
      messages: [msg("1", "1"), msg("2", "2")],
      next_cursor: null,
    });
    await mail.loadInitialMessages();
    expect(mail.snapshot.messages).toHaveLength(2);
  });

  it("setSelection updates current selection", () => {
    mocks.listMessages.mockResolvedValue({ messages: [], next_cursor: null });
    mail.setSelection({ kind: "account", accountId: "1" });
    expect(mail.snapshot.selection).toEqual({ kind: "account", accountId: "1" });
  });

  it("openMessage loads detail and stores it", async () => {
    mocks.getMessage.mockResolvedValue(detail("42", "plain body"));
    await mail.openMessage("42");
    expect(mail.snapshot.selectedMessage?.id).toBe("42");
    expect(mail.snapshot.selectedMessage?.body_text).toBe("plain body");
  });

  it("openMessage with same id is a no-op (no extra fetch)", async () => {
    mocks.getMessage.mockResolvedValue(detail("42", "x"));
    await mail.openMessage("42");
    await mail.openMessage("42");
    expect(mocks.getMessage).toHaveBeenCalledTimes(1);
  });

  it("loadingMessages is true during fetch, false after", async () => {
    let resolveFn!: (v: { messages: MessageSummary[]; next_cursor: string | null }) => void;
    mocks.listMessages.mockReturnValue(
      new Promise((r) => {
        resolveFn = r;
      }),
    );
    const pending = mail.loadInitialMessages();
    expect(mail.snapshot.loadingMessages).toBe(true);
    resolveFn({ messages: [], next_cursor: null });
    await pending;
    expect(mail.snapshot.loadingMessages).toBe(false);
  });

  it("accepts null next_cursor without error", async () => {
    mocks.listMessages.mockResolvedValue({
      messages: [msg("1", "1")],
      next_cursor: null,
    });
    await mail.loadInitialMessages();
    expect(mail.snapshot.messages).toHaveLength(1);
    expect(mail.snapshot.errorMessage).toBeNull();
  });

  it("captures errorMessage on load failure", async () => {
    mocks.listMessages.mockRejectedValue({ kind: "Auth", detail: "NotLoggedIn" });
    await mail.loadInitialMessages();
    expect(mail.snapshot.errorMessage).toContain("Auth");
  });

  it("reset clears everything", async () => {
    mocks.listAccounts.mockResolvedValue([acct("1", "alice")]);
    await mail.loadAccounts();
    mail.reset();
    expect(mail.snapshot.accounts).toEqual([]);
    expect(mail.snapshot.selection).toEqual({ kind: "all" });
  });

  it("loadInitialMessages sets messagesCursor from next_cursor", async () => {
    mocks.listMessages.mockResolvedValue({
      messages: [msg("1", "1")],
      next_cursor: "cur-42",
    });
    await mail.loadInitialMessages();
    expect(mail.messagesCursor).toBe("cur-42");
  });

  it("loadInitialMessages clears messagesCursor when next_cursor is null", async () => {
    mocks.listMessages.mockResolvedValueOnce({
      messages: [msg("1", "1")],
      next_cursor: "cur-7",
    });
    await mail.loadInitialMessages();
    mocks.listMessages.mockResolvedValueOnce({
      messages: [],
      next_cursor: null,
    });
    await mail.loadInitialMessages();
    expect(mail.messagesCursor).toBeNull();
  });
});

describe("mail.mergePendingNewMessages_internal", () => {
  it("pushes fresh messages into pendingNewMessages and returns the count added", () => {
    const a = msg("1", "1");
    const b = msg("2", "1");
    const c = msg("3", "1");
    mail.mergePendingNewMessages_internal([a]);
    const added = mail.mergePendingNewMessages_internal([a, b, c]);
    expect(added).toBe(2);
    // Incoming order preserved; newer call's fresh items prepend into buffer.
    // After first call: pending = ["1"]. After second: fresh = ["2","3"],
    // merged = ["2","3","1"].
    expect(mail.snapshot.pendingNewMessages.map((m) => m.message_id)).toEqual(["2", "3", "1"]);
    // messages list is untouched.
    expect(mail.snapshot.messages).toHaveLength(0);
  });

  it("returns 0 and leaves state untouched when all incoming are duplicates", () => {
    mail.mergePendingNewMessages_internal([msg("1", "1"), msg("2", "1")]);
    const before = mail.snapshot.pendingNewMessages;
    const added = mail.mergePendingNewMessages_internal([msg("1", "1"), msg("2", "1")]);
    expect(added).toBe(0);
    expect(mail.snapshot.pendingNewMessages).toBe(before);
  });

  it("caps the buffer at MAX_PENDING_NEW_MESSAGES so a long-running poll loop cannot leak memory", () => {
    // Seed past the cap so the trim path is exercised, then verify the
    // most-recent prefix is kept and the tail is dropped. Without the cap
    // a 30s poller would accumulate state unboundedly.
    const ids = Array.from({ length: MAX_PENDING_NEW_MESSAGES + 50 }, (_, i) => String(i));
    mail.mergePendingNewMessages_internal(ids.map((id) => msg(id, "1")));
    expect(mail.snapshot.pendingNewMessages.length).toBe(MAX_PENDING_NEW_MESSAGES);
    expect(mail.snapshot.pendingNewMessages[0].message_id).toBe("0");
    expect(mail.snapshot.pendingNewMessages[MAX_PENDING_NEW_MESSAGES - 1].message_id).toBe(
      String(MAX_PENDING_NEW_MESSAGES - 1),
    );
  });
});

describe("mail.pollOnce", () => {
  it("invokes list_recent_messages_cmd with the current cursor", async () => {
    // Prime the changeCursor via a first pollOnce call.
    mocks.invoke.mockResolvedValueOnce({ new_messages: [msg("1", "1")], next_cursor: "cur-1" });
    await mail.pollOnce();
    mocks.invoke.mockResolvedValue({ new_messages: [], next_cursor: "cur-1" });
    await mail.pollOnce();
    expect(mocks.invoke).toHaveBeenCalledWith("list_recent_messages_cmd", { since: "cur-1" });
  });

  it("routes fresh messages to pendingNewMessages (not messages) and advances the cursor", async () => {
    // Prime cursor via first pollOnce.
    mocks.invoke.mockResolvedValueOnce({ new_messages: [msg("1", "1")], next_cursor: "cur-1" });
    await mail.pollOnce();
    mocks.invoke.mockResolvedValue({
      new_messages: [msg("1", "1"), msg("2", "1"), msg("3", "1")],
      next_cursor: "cur-3",
    });
    await mail.pollOnce();
    // messages list stays empty; polled items go to pendingNewMessages.
    expect(mail.snapshot.messages).toHaveLength(0);
    expect(mail.snapshot.pendingNewMessages.map((m) => m.message_id)).toEqual(["2", "3", "1"]);
    expect(mail.changeCursor).toBe("cur-3");
  });

  it("keeps the previous cursor when next_cursor is empty string", async () => {
    // Prime cursor via first pollOnce.
    mocks.invoke.mockResolvedValueOnce({ new_messages: [], next_cursor: "cur-stable" });
    await mail.pollOnce();
    mocks.invoke.mockResolvedValue({ new_messages: [], next_cursor: "" });
    await mail.pollOnce();
    expect(mail.changeCursor).toBe("cur-stable");
  });

  it("captures errorMessage when the invoke rejects", async () => {
    mocks.invoke.mockRejectedValue({ kind: "Http", detail: "boom" });
    await mail.pollOnce();
    expect(mail.snapshot.errorMessage).toContain("Http");
  });

  it("does not throw when called before loadInitialMessages (cursor=null)", async () => {
    mocks.invoke.mockResolvedValue({ new_messages: [msg("9", "1")], next_cursor: "cur-9" });
    await mail.pollOnce();
    expect(mocks.invoke).toHaveBeenCalledWith("list_recent_messages_cmd", { since: null });
    // Polled item goes to pendingNewMessages, not messages.
    expect(mail.snapshot.messages).toHaveLength(0);
    expect(mail.snapshot.pendingNewMessages.map((m) => m.message_id)).toEqual(["9"]);
    expect(mail.changeCursor).toBe("cur-9");
  });

  it("increments pollFailureCount on rejection and resets it on success", async () => {
    mocks.invoke.mockRejectedValueOnce({ kind: "Http", detail: "boom" });
    await mail.pollOnce();
    expect(mail.pollFailureCount).toBe(1);
    mocks.invoke.mockRejectedValueOnce({ kind: "Http", detail: "boom" });
    await mail.pollOnce();
    expect(mail.pollFailureCount).toBe(2);
    mocks.invoke.mockResolvedValueOnce({ new_messages: [], next_cursor: null });
    await mail.pollOnce();
    expect(mail.pollFailureCount).toBe(0);
  });

  it("stops the polling loop after MAX_POLL_FAILURES consecutive failures", async () => {
    mocks.invoke.mockRejectedValue({ kind: "Auth", detail: "NotLoggedIn" });
    mail.startPolling();
    expect(mail.isPolling).toBe(true);
    // pollOnce is called manually here (not via timer) so we don't have to
    // juggle fake timers; the behavior under test is the failure-count gate.
    for (let i = 0; i < MAX_POLL_FAILURES; i++) {
      await mail.pollOnce();
    }
    expect(mail.isPolling).toBe(false);
    expect(mail.snapshot.errorMessage).toContain(`${MAX_POLL_FAILURES} consecutive failures`);
    mail.stopPolling();
  });
});

describe("mail.startPolling / stopPolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    mail.stopPolling();
    vi.useRealTimers();
  });

  it("startPolling triggers pollOnce on every interval tick", async () => {
    mocks.invoke.mockResolvedValue({ new_messages: [], next_cursor: null });
    mail.startPolling();
    expect(mail.isPolling).toBe(true);
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(mocks.invoke).toHaveBeenCalledTimes(3);
  });

  it("startPolling is idempotent — second call does not spawn a second interval", async () => {
    mocks.invoke.mockResolvedValue({ new_messages: [], next_cursor: null });
    mail.startPolling();
    mail.startPolling();
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(mocks.invoke).toHaveBeenCalledTimes(1);
  });

  it("stopPolling halts further ticks", async () => {
    mocks.invoke.mockResolvedValue({ new_messages: [], next_cursor: null });
    mail.startPolling();
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(mocks.invoke).toHaveBeenCalledTimes(1);
    mail.stopPolling();
    expect(mail.isPolling).toBe(false);
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5);
    expect(mocks.invoke).toHaveBeenCalledTimes(1);
  });

  it("reset stops the polling loop", () => {
    mail.startPolling();
    expect(mail.isPolling).toBe(true);
    mail.reset();
    expect(mail.isPolling).toBe(false);
  });
});

// --- new tests for paginated browse ---

describe("loadInitialMessages", () => {
  beforeEach(() => {
    mail.reset();
    vi.restoreAllMocks();
  });

  it("populates messages from /v1/messages and sets messagesHasMore from next_cursor", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [
        { message_id: "1", subject: "a", from: { address: null, name: null },
          date: null, account: { id: "1", name: "x" } },
      ],
      next_cursor: "cur-1",
    });
    await mail.loadInitialMessages();
    const snap = mail.snapshot;
    expect(snap.messages.map((m) => m.message_id)).toEqual(["1"]);
    expect(mail.messagesCursor).toBe("cur-1");
    expect(mail.messagesHasMore).toBe(true);
  });

  it("sets messagesHasMore=false when next_cursor is null", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    await mail.loadInitialMessages();
    expect(mail.messagesHasMore).toBe(false);
    expect(mail.messagesCursor).toBeNull();
  });
});

describe("loadMoreMessages", () => {
  beforeEach(() => {
    mail.reset();
    vi.restoreAllMocks();
  });

  it("appends results and advances cursor", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages")
      .mockResolvedValueOnce({
        messages: [
          { message_id: "1", subject: "a", from: { address: null, name: null },
            date: null, account: { id: "1", name: "x" } },
        ],
        next_cursor: "cur-1",
      })
      .mockResolvedValueOnce({
        messages: [
          { message_id: "2", subject: "b", from: { address: null, name: null },
            date: null, account: { id: "1", name: "x" } },
        ],
        next_cursor: null,
      });
    await mail.loadInitialMessages();
    await mail.loadMoreMessages();
    expect(spy).toHaveBeenCalledTimes(2);
    expect(mail.snapshot.messages.map((m) => m.message_id)).toEqual(["1", "2"]);
    expect(mail.messagesHasMore).toBe(false);
  });

  it("is a no-op when messagesHasMore is false", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages")
      .mockResolvedValue({ messages: [], next_cursor: null });
    await mail.loadInitialMessages();      // sets hasMore=false
    spy.mockClear();
    await mail.loadMoreMessages();          // should not fire
    expect(spy).not.toHaveBeenCalled();
  });

  it("two concurrent calls fire one network request", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages")
      .mockResolvedValueOnce({
        messages: [], next_cursor: "cur-1",
      })
      .mockResolvedValue({
        messages: [{ message_id: "9", subject: "z",
                     from: { address: null, name: null }, date: null,
                     account: { id: "1", name: "x" } }],
        next_cursor: null,
      });
    await mail.loadInitialMessages();
    spy.mockClear();
    spy.mockResolvedValue({
      messages: [{ message_id: "9", subject: "z",
                   from: { address: null, name: null }, date: null,
                   account: { id: "1", name: "x" } }],
      next_cursor: null,
    });
    await Promise.all([mail.loadMoreMessages(), mail.loadMoreMessages()]);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("re-uses the current filter opts on subsequent loadMore calls", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages")
      .mockResolvedValueOnce({
        messages: [{ message_id: "1", subject: "a",
          from: { address: null, name: null }, date: null,
          account: { id: "7", name: "scoped" } }],
        next_cursor: "cur-1",
      })
      .mockResolvedValueOnce({
        messages: [], next_cursor: null,
      });
    await mail.loadInitialMessages({ accountIds: ["7"] });
    await mail.loadMoreMessages();
    expect(spy).toHaveBeenLastCalledWith({
      account_ids: ["7"], folder_ids: [], limit: 50, cursor: "cur-1",
    });
  });
});

describe("setSelection refetches from /v1/messages", () => {
  beforeEach(() => {
    mail.reset();
    vi.restoreAllMocks();
  });

  it("calls listMessages with the selected account's id", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    mail.setSelection({ kind: "account", accountId: "42" });
    // setSelection fires the load without awaiting; flush microtasks.
    await Promise.resolve();
    await Promise.resolve();
    expect(spy).toHaveBeenCalledWith({
      account_ids: ["42"], folder_ids: [], limit: 50, cursor: null,
    });
  });

  it("calls listMessages with both account_id and folder_id when a folder is selected", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    mail.setSelection({ kind: "folder", accountId: "42", folderId: "9" });
    await Promise.resolve();
    await Promise.resolve();
    expect(spy).toHaveBeenCalledWith({
      account_ids: ["42"], folder_ids: ["9"], limit: 50, cursor: null,
    });
  });

  it("is a no-op when the selection is unchanged", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    mail.setSelection({ kind: "account", accountId: "42" });
    await Promise.resolve(); await Promise.resolve();
    spy.mockClear();
    mail.setSelection({ kind: "account", accountId: "42" });
    await Promise.resolve(); await Promise.resolve();
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("pendingNewMessages buffer", () => {
  beforeEach(() => {
    mail.reset();
    vi.restoreAllMocks();
  });

  it("pollOnce pushes into pendingNewMessages, not messages", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "11",
    });
    await mail.pollOnce();
    expect(mail.snapshot.messages).toHaveLength(0);
    expect(mail.snapshot.pendingNewMessages).toHaveLength(1);
  });

  it("dedups against both messages and pendingNewMessages", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [
        { message_id: "5", subject: "old",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        // Same as the one already in messages — must NOT appear in pending.
        { message_id: "5", subject: "old",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "11",
    });
    await mail.pollOnce();
    expect(mail.snapshot.pendingNewMessages.map((m) => m.message_id)).toEqual(["10"]);
  });

  it("mergePendingNewMessages prepends and clears", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [
        { message_id: "5", subject: "old",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "11",
    });
    await mail.pollOnce();
    mail.mergePendingNewMessages();
    expect(mail.snapshot.messages.map((m) => m.message_id)).toEqual(["10", "5"]);
    expect(mail.snapshot.pendingNewMessages).toEqual([]);
  });
});
