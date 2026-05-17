import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  getMessage: vi.fn(),
}));

vi.mock("../tauri", () => mocks);

import { mail } from "./mail.svelte";
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
});
