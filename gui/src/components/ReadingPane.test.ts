import { describe, expect, it, beforeEach } from "vitest";
import { render, screen } from "@testing-library/svelte";

import ReadingPane from "./ReadingPane.svelte";
import { mail } from "../lib/stores/mail.svelte";

beforeEach(() => {
  mail.reset();
});

describe("ReadingPane", () => {
  it("shows empty state when no message is selected", () => {
    const { getByText } = render(ReadingPane);
    expect(getByText(/select a message/i)).toBeTruthy();
  });

  it("renders subject, from, plain-text body when a message is open", () => {
    mail.snapshot.selectedMessage = {
      id: "1",
      subject: "School excursion",
      from: { name: "Anna H.", address: "anna@example.com" },
      to: [{ name: null, address: "horst@example.com" }],
      cc: [],
      bcc: [],
      date: "2026-05-17T09:00:00Z",
      body_text: "Bus leaves at 7:30",
      body_html: null,
      attachments: [],
      account: { id: "1", name: "personal", address: "horst@example.com" },
      folders: [{ id: "10", name: "INBOX" }],
    };
    mail.snapshot.bodyMode = "plain";
    const { getByText } = render(ReadingPane);
    expect(getByText("School excursion")).toBeTruthy();
    expect(getByText(/anna h\./i)).toBeTruthy();
    expect(getByText("Bus leaves at 7:30")).toBeTruthy();
  });

  it("shows a placeholder when both body parts are null", () => {
    mail.snapshot.selectedMessage = {
      id: "1",
      subject: "Empty body",
      from: { name: null, address: null },
      to: [],
      cc: [],
      bcc: [],
      date: null,
      body_text: null,
      body_html: null,
      attachments: [],
      account: { id: "1", name: null, address: null },
      folders: [],
    };
    const { getByText } = render(ReadingPane);
    expect(getByText(/no html body available/i)).toBeTruthy();
  });
});

describe("ReadingPane body-mode toggle", () => {
  beforeEach(() => { mail.reset(); });

  it("renders HtmlBody when bodyMode=html and body_html present", async () => {
    const { container } = render(ReadingPane);
    (mail as any).snapshot.selectedMessage = {
      id: "1", subject: "Hi", from: { name: null, address: "x@x" },
      to: [], cc: [], bcc: [], date: null,
      body_text: "plain", body_html: "<p>html</p>",
      attachments: [], account: { id: "1", name: null, address: null }, folders: [],
    };
    (mail as any).snapshot.bodyMode = "html";
    await Promise.resolve();
    expect(container.querySelector("iframe")).toBeTruthy();
  });

  it("Load images button visible when bodyMode=html and not yet allowed", async () => {
    render(ReadingPane);
    (mail as any).snapshot.selectedMessage = {
      id: "1", subject: null, from: { name: null, address: null },
      to: [], cc: [], bcc: [], date: null,
      body_text: null, body_html: "<p>x</p>", attachments: [],
      account: { id: "1", name: null, address: null }, folders: [],
    };
    (mail as any).snapshot.bodyMode = "html";
    await Promise.resolve();
    expect(screen.getByRole("button", { name: /load images/i })).toBeTruthy();
  });
});
