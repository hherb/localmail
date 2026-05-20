import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent, screen } from "@testing-library/svelte";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  getMessage: vi.fn(),
  runSearch: vi.fn(async () => ({
    results: [],
    next_cursor: null,
    total_estimate: null,
    took_ms: 0,
  })),
}));

vi.mock("../lib/tauri", () => mocks);

import AccountTree from "./AccountTree.svelte";
import { mail } from "../lib/stores/mail.svelte";
import { search } from "../lib/stores/search.svelte";

beforeEach(() => {
  mail.reset();
  search.reset();
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

describe("AccountTree dispatches server-side search on selection", () => {
  it("clicking an account writes accountIds and submits", async () => {
    (mail as unknown as { snapshot: { accounts: unknown[] } }).snapshot.accounts = [
      {
        id: "5",
        name: "gmail.com",
        address: "a@gmail.com",
        last_sync_at: null,
        message_count: 100,
        capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
      },
    ];
    mocks.listFolders.mockResolvedValue([]);
    render(AccountTree);
    await fireEvent.click(screen.getByText("gmail.com"));
    expect(search.snapshot.filters.accountIds).toEqual(["5"]);
    expect(mocks.runSearch).toHaveBeenCalled();
  });

  it("clicking All Mail resets the search store (no submit)", async () => {
    // "All Mail" is the "go home, show all recent mail" affordance. It must
    // NOT fire a search — an empty-query search degenerates to vector-arm
    // hits against the embedding of the empty string, which surfaces
    // exactly `rerank_pool_size` (default 20) arbitrary-looking results.
    // Resetting the store clears `tookMs`, flipping MessageList back to the
    // mail.messages (recent-by-date) view.
    search.setFilters({ ...search.snapshot.filters, accountIds: ["5"], folderIds: ["42"] });
    // Seed tookMs so we can assert reset() was the path taken.
    const { __setSearchResultsForTest } = await import("../lib/stores/search.svelte");
    __setSearchResultsForTest([], 12);
    render(AccountTree);
    await fireEvent.click(screen.getByText(/all mail/i));
    expect(search.snapshot.filters.accountIds).toEqual([]);
    expect(search.snapshot.filters.folderIds).toEqual([]);
    expect(search.snapshot.tookMs).toBeNull();
    expect(mocks.runSearch).not.toHaveBeenCalled();
  });
});
