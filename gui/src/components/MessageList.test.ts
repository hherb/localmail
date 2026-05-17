import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/svelte";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  getMessage: vi.fn(),
}));

vi.mock("../lib/tauri", () => mocks);

import MessageList from "./MessageList.svelte";
import { mail } from "../lib/stores/mail.svelte";

beforeEach(() => {
  mail.reset();
  vi.clearAllMocks();
});

describe("MessageList", () => {
  it("shows an empty hint when no messages loaded", () => {
    const { getByText } = render(MessageList);
    expect(getByText(/no messages/i)).toBeTruthy();
  });

  it("renders a row per loaded message under selection=all", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [
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
      next_cursor: "2",
    });
    await mail.loadRecentMessages();
    const { getByText } = render(MessageList);
    expect(getByText("hi anna")).toBeTruthy();
    expect(getByText("second")).toBeTruthy();
  });

  it("narrows to one account when selection=account", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [
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
      next_cursor: "2",
    });
    await mail.loadRecentMessages();
    mail.setSelection({ kind: "account", accountId: "1" });
    const { getByText, queryByText } = render(MessageList);
    expect(getByText("hi anna")).toBeTruthy();
    expect(queryByText("second")).toBeNull();
  });

  it("renders error message when store.errorMessage is set after a failed load", async () => {
    mocks.listRecentMessages.mockRejectedValue({ kind: "Http", detail: "boom" });
    await mail.loadRecentMessages();
    const { getByText } = render(MessageList);
    expect(getByText(/boom/i)).toBeTruthy();
  });

  it("clicking a row calls openMessage with its id", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [
        {
          message_id: "42",
          subject: "click me",
          from: { name: "X", address: null },
          date: null,
          account: { id: "1", name: "p" },
        },
      ],
      next_cursor: "42",
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
    await mail.loadRecentMessages();
    const { getByText } = render(MessageList);
    await fireEvent.click(getByText("click me"));
    expect(mocks.getMessage).toHaveBeenCalledWith("42");
  });
});
