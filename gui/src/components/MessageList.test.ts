import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent, screen } from "@testing-library/svelte";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  listMessages: vi.fn(),
  getMessage: vi.fn(),
}));

vi.mock("../lib/tauri", () => mocks);

import MessageList from "./MessageList.svelte";
import { mail } from "../lib/stores/mail.svelte";
import { search } from "../lib/stores/search.svelte";
import { __setSearchResultsForTest } from "../lib/stores/search.svelte";

beforeEach(() => {
  mail.reset();
  search.reset();
  vi.clearAllMocks();
  // Default no-op so setSelection / loadInitialMessages calls don't fail.
  mocks.listMessages.mockResolvedValue({ messages: [], next_cursor: null });
});

describe("MessageList", () => {
  it("shows an empty hint when no messages loaded", () => {
    const { getByText } = render(MessageList);
    expect(getByText(/no messages/i)).toBeTruthy();
  });

  it("renders a row per loaded message under selection=all", async () => {
    mocks.listMessages.mockResolvedValue({
      messages: [
        {
          message_id: "1",
          subject: "hi anna",
          from: { name: "Anna", address: "anna@x" },
          date: null,
          account: { id: "1", name: "personal" },
        },
        {
          message_id: "2",
          subject: "second",
          from: { name: null, address: "bob@x" },
          date: null,
          account: { id: "2", name: "work" },
        },
      ],
      next_cursor: null,
    });
    await mail.loadInitialMessages();
    const { getByText } = render(MessageList);
    expect(getByText("hi anna")).toBeTruthy();
    expect(getByText("second")).toBeTruthy();
  });

  it("narrows to one account when selection=account", async () => {
    mocks.listMessages.mockResolvedValueOnce({
      messages: [
        {
          message_id: "1",
          subject: "hi anna",
          from: { name: "Anna", address: "anna@x" },
          date: null,
          account: { id: "1", name: "personal" },
        },
        {
          message_id: "2",
          subject: "second",
          from: { name: null, address: "bob@x" },
          date: null,
          account: { id: "2", name: "work" },
        },
      ],
      next_cursor: null,
    });
    await mail.loadInitialMessages();
    // setSelection triggers a new loadInitialMessages with account filter;
    // the default mock returns [] for that call.
    mocks.listMessages.mockResolvedValueOnce({
      messages: [
        {
          message_id: "1",
          subject: "hi anna",
          from: { name: "Anna", address: "anna@x" },
          date: null,
          account: { id: "1", name: "personal" },
        },
      ],
      next_cursor: null,
    });
    mail.setSelection({ kind: "account", accountId: "1" });
    // Flush the async loadInitialMessages triggered by setSelection.
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    const { getByText, queryByText } = render(MessageList);
    expect(getByText("hi anna")).toBeTruthy();
    expect(queryByText("second")).toBeNull();
  });

  it("renders error message when store.errorMessage is set after a failed load", async () => {
    mocks.listMessages.mockRejectedValue({ kind: "Http", detail: "boom" });
    await mail.loadInitialMessages();
    const { getByText } = render(MessageList);
    expect(getByText(/boom/i)).toBeTruthy();
  });

  it("clicking a row calls openMessage with its id", async () => {
    mocks.listMessages.mockResolvedValue({
      messages: [
        {
          message_id: "42",
          subject: "click me",
          from: { name: "X", address: null },
          date: null,
          account: { id: "1", name: "p" },
        },
      ],
      next_cursor: null,
    });
    mocks.getMessage.mockResolvedValue({
      id: "42",
      subject: "click me",
      from: { name: null, address: null },
      to: [],
      cc: [],
      bcc: [],
      date: null,
      body_text: "body",
      body_html: null,
      attachments: [],
      account: { id: "1", name: null, address: null },
      folders: [],
    });
    await mail.loadInitialMessages();
    const { getByText } = render(MessageList);
    await fireEvent.click(getByText("click me"));
    expect(mocks.getMessage).toHaveBeenCalledWith("42");
  });
});

describe("MessageList with search results", () => {
  it("renders search.results when present, with snippet text", () => {
    search.setQuery("hello");
    __setSearchResultsForTest(
      [
        {
          message_id: "1",
          account: { id: "1", name: "gmail" },
          folder: null,
          subject: "Re: school",
          from: { name: "Anna", address: "a@x" },
          to: [],
          date: null,
          snippet_html: "…leaves at <mark>7:30</mark>…",
          has_attachments: false,
          score: 0.5,
          matched_arms: ["bm25"],
        },
      ],
      42.0,
    );
    render(MessageList);
    expect(screen.getByText(/Re: school/)).toBeTruthy();
    expect(screen.getByText(/Search took/)).toBeTruthy();
    expect(screen.getByText(/7:30/).tagName.toLowerCase()).toBe("mark");
  });

  it("renders 'no matches' when results is empty and a query was submitted", () => {
    search.setQuery("xyz");
    __setSearchResultsForTest([], 5.0);
    render(MessageList);
    expect(screen.getByText(/no matches/i)).toBeTruthy();
  });
});

describe("MessageList — pagination affordances", () => {
  beforeEach(() => {
    mail.reset();
    search.reset();
  });

  it("renders a Load more button when mail has more pages", async () => {
    mocks.listMessages.mockResolvedValue({
      messages: [
        { message_id: "1", subject: "a",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "tok",
    });
    await mail.loadInitialMessages();
    render(MessageList);
    expect(screen.getByRole("button", { name: /load more/i })).toBeTruthy();
  });

  it("Load more button fires mail.loadMoreMessages when not searching", async () => {
    mocks.listMessages
      .mockResolvedValueOnce({
        messages: [
          { message_id: "1", subject: "a",
            from: { address: null, name: null }, date: null,
            account: { id: "1", name: "x" } },
        ],
        next_cursor: "tok",
      })
      .mockResolvedValueOnce({
        messages: [
          { message_id: "2", subject: "b",
            from: { address: null, name: null }, date: null,
            account: { id: "1", name: "x" } },
        ],
        next_cursor: null,
      });
    await mail.loadInitialMessages();
    render(MessageList);
    const btn = screen.getByRole("button", { name: /load more/i });
    await fireEvent.click(btn);
    expect(mail.snapshot.messages).toHaveLength(2);
  });

  it("renders 'N new messages' banner when pendingNewMessages is non-empty", async () => {
    mocks.listMessages.mockResolvedValue({
      messages: [], next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../lib/api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
        { message_id: "11", subject: "fresh2",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "12",
    });
    await mail.pollOnce();
    render(MessageList);
    expect(screen.getByText(/2 new messages/i)).toBeTruthy();
  });

  it("clicking the banner merges pendingNewMessages and dismisses", async () => {
    mocks.listMessages.mockResolvedValue({
      messages: [], next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../lib/api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "11",
    });
    await mail.pollOnce();
    render(MessageList);
    await fireEvent.click(screen.getByText(/1 new message/i));
    expect(mail.snapshot.pendingNewMessages).toEqual([]);
    expect(mail.snapshot.messages).toHaveLength(1);
  });
});
