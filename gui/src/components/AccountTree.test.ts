import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/svelte";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  getMessage: vi.fn(),
}));

vi.mock("../lib/tauri", () => mocks);

import AccountTree from "./AccountTree.svelte";
import { mail } from "../lib/stores/mail.svelte";

beforeEach(() => {
  mail.reset();
  vi.clearAllMocks();
});

describe("AccountTree", () => {
  it('renders "All Mail" pinned entry', () => {
    const { getByText } = render(AccountTree);
    expect(getByText(/all mail/i)).toBeTruthy();
  });

  it("renders account names from the store", async () => {
    mocks.listAccounts.mockResolvedValue([
      {
        id: "1",
        name: "personal",
        address: null,
        last_sync_at: null,
        message_count: 0,
        capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
      },
    ]);
    await mail.loadAccounts();
    const { getByText } = render(AccountTree);
    expect(getByText("personal")).toBeTruthy();
  });

  it("clicking an account toggles folder list and calls loadFoldersFor", async () => {
    mocks.listAccounts.mockResolvedValue([
      {
        id: "1",
        name: "personal",
        address: null,
        last_sync_at: null,
        message_count: 0,
        capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
      },
    ]);
    mocks.listFolders.mockResolvedValue([
      {
        id: "10",
        name: "INBOX",
        full_path: "INBOX",
        flags: null,
        last_uid: null,
        message_count: 0,
      },
    ]);
    await mail.loadAccounts();
    const { getByText, findByText } = render(AccountTree);
    await fireEvent.click(getByText("personal"));
    expect(mocks.listFolders).toHaveBeenCalledWith("1");
    expect(await findByText("INBOX")).toBeTruthy();
  });

  it("rapid second click while folders loading does not collapse the tree", async () => {
    mocks.listAccounts.mockResolvedValue([
      {
        id: "1",
        name: "personal",
        address: null,
        last_sync_at: null,
        message_count: 0,
        capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
      },
    ]);
    let resolveFolders!: (v: unknown) => void;
    mocks.listFolders.mockReturnValue(
      new Promise((r) => {
        resolveFolders = r;
      }),
    );
    await mail.loadAccounts();
    const { getByText, findByText } = render(AccountTree);
    await fireEvent.click(getByText("personal"));
    await fireEvent.click(getByText("personal"));
    resolveFolders([
      { id: "10", name: "INBOX", full_path: "INBOX", flags: null, last_uid: null, message_count: 0 },
    ]);
    expect(await findByText("INBOX")).toBeTruthy();
    expect(mocks.listFolders).toHaveBeenCalledTimes(1);
  });

  it("clicking a folder sets selection.kind = folder", async () => {
    mocks.listAccounts.mockResolvedValue([
      {
        id: "1",
        name: "personal",
        address: null,
        last_sync_at: null,
        message_count: 0,
        capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
      },
    ]);
    mocks.listFolders.mockResolvedValue([
      {
        id: "10",
        name: "INBOX",
        full_path: "INBOX",
        flags: null,
        last_uid: null,
        message_count: 0,
      },
    ]);
    await mail.loadAccounts();
    const { getByText, findByText } = render(AccountTree);
    await fireEvent.click(getByText("personal"));
    await fireEvent.click(await findByText("INBOX"));
    expect(mail.snapshot.selection).toEqual({
      kind: "folder",
      accountId: "1",
      folderId: "10",
    });
  });
});
