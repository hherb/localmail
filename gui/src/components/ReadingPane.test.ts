import { describe, expect, it, beforeEach } from "vitest";
import { render } from "@testing-library/svelte";

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
    const { getByText } = render(ReadingPane);
    expect(getByText("School excursion")).toBeTruthy();
    expect(getByText(/anna h\./i)).toBeTruthy();
    expect(getByText("Bus leaves at 7:30")).toBeTruthy();
  });

  it("shows a placeholder when body_text is null", () => {
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
    expect(getByText(/no plain-text body/i)).toBeTruthy();
  });
});
