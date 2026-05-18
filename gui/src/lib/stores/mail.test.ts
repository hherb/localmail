import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  getMessage: vi.fn(),
  invoke: vi.fn(),
}));

vi.mock("../tauri", () => mocks);
vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));

import { mail } from "./mail.svelte";
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

  it("loads recent messages and exposes them via snapshot.messages", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [msg("1", "1"), msg("2", "2")],
      next_cursor: "2",
    });
    await mail.loadRecentMessages();
    expect(mail.snapshot.messages).toHaveLength(2);
  });

  it("setSelection updates current selection", () => {
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
    let resolveFn!: (v: { new_messages: MessageSummary[]; next_cursor: string | null }) => void;
    mocks.listRecentMessages.mockReturnValue(
      new Promise((r) => {
        resolveFn = r;
      }),
    );
    const pending = mail.loadRecentMessages();
    expect(mail.snapshot.loadingMessages).toBe(true);
    resolveFn({ new_messages: [], next_cursor: null });
    await pending;
    expect(mail.snapshot.loadingMessages).toBe(false);
  });

  it("accepts null next_cursor without error", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [msg("1", "1")],
      next_cursor: null,
    });
    await mail.loadRecentMessages();
    expect(mail.snapshot.messages).toHaveLength(1);
    expect(mail.snapshot.errorMessage).toBeNull();
  });

  it("captures errorMessage on load failure", async () => {
    mocks.listRecentMessages.mockRejectedValue({ kind: "Auth", detail: "NotLoggedIn" });
    await mail.loadRecentMessages();
    expect(mail.snapshot.errorMessage).toContain("Auth");
  });

  it("reset clears everything", async () => {
    mocks.listAccounts.mockResolvedValue([acct("1", "alice")]);
    await mail.loadAccounts();
    mail.reset();
    expect(mail.snapshot.accounts).toEqual([]);
    expect(mail.snapshot.selection).toEqual({ kind: "all" });
  });

  it("loadRecentMessages captures next_cursor for later polling", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [msg("1", "1")],
      next_cursor: "cur-42",
    });
    await mail.loadRecentMessages();
    expect(mail.changeCursor).toBe("cur-42");
  });

  it("loadRecentMessages preserves prior cursor when next_cursor is null", async () => {
    mocks.listRecentMessages.mockResolvedValueOnce({
      new_messages: [msg("1", "1")],
      next_cursor: "cur-7",
    });
    await mail.loadRecentMessages();
    mocks.listRecentMessages.mockResolvedValueOnce({
      new_messages: [],
      next_cursor: null,
    });
    await mail.loadRecentMessages();
    expect(mail.changeCursor).toBe("cur-7");
  });
});

describe("mail.mergeNewMessages", () => {
  it("prepends fresh messages and returns the count added", () => {
    const a = msg("1", "1");
    const b = msg("2", "1");
    const c = msg("3", "1");
    mail.mergeNewMessages([a]);
    const added = mail.mergeNewMessages([a, b, c]);
    expect(added).toBe(2);
    // dedupNewMessages preserves the incoming server order; merged result
    // is [<new in given order>, ...existing].
    expect(mail.snapshot.messages.map((m) => m.message_id)).toEqual(["2", "3", "1"]);
  });

  it("returns 0 and leaves state untouched when all incoming are duplicates", () => {
    mail.mergeNewMessages([msg("1", "1"), msg("2", "1")]);
    const before = mail.snapshot.messages;
    const added = mail.mergeNewMessages([msg("1", "1"), msg("2", "1")]);
    expect(added).toBe(0);
    expect(mail.snapshot.messages).toBe(before);
  });
});

describe("mail.pollOnce", () => {
  it("invokes list_recent_messages_cmd with the current cursor", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [msg("1", "1")],
      next_cursor: "cur-1",
    });
    await mail.loadRecentMessages();
    mocks.invoke.mockResolvedValue({ new_messages: [], next_cursor: "cur-1" });
    await mail.pollOnce();
    expect(mocks.invoke).toHaveBeenCalledWith("list_recent_messages_cmd", { since: "cur-1" });
  });

  it("merges fresh messages and advances the cursor", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [msg("1", "1")],
      next_cursor: "cur-1",
    });
    await mail.loadRecentMessages();
    mocks.invoke.mockResolvedValue({
      new_messages: [msg("1", "1"), msg("2", "1"), msg("3", "1")],
      next_cursor: "cur-3",
    });
    await mail.pollOnce();
    expect(mail.snapshot.messages.map((m) => m.message_id)).toEqual(["2", "3", "1"]);
    expect(mail.changeCursor).toBe("cur-3");
  });

  it("keeps the previous cursor when next_cursor is empty string", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [],
      next_cursor: "cur-stable",
    });
    await mail.loadRecentMessages();
    mocks.invoke.mockResolvedValue({ new_messages: [], next_cursor: "" });
    await mail.pollOnce();
    expect(mail.changeCursor).toBe("cur-stable");
  });

  it("captures errorMessage when the invoke rejects", async () => {
    mocks.invoke.mockRejectedValue({ kind: "Http", detail: "boom" });
    await mail.pollOnce();
    expect(mail.snapshot.errorMessage).toContain("Http");
  });

  it("does not throw when called before loadRecentMessages (cursor=null)", async () => {
    mocks.invoke.mockResolvedValue({ new_messages: [msg("9", "1")], next_cursor: "cur-9" });
    await mail.pollOnce();
    expect(mocks.invoke).toHaveBeenCalledWith("list_recent_messages_cmd", { since: null });
    expect(mail.snapshot.messages.map((m) => m.message_id)).toEqual(["9"]);
    expect(mail.changeCursor).toBe("cur-9");
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
