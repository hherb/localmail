import { describe, expect, it, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/svelte";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async () => {
    throw new Error("invoke should be mocked at the api wrapper level");
  }),
}));

import ReadingPane from "./ReadingPane.svelte";
import { mail } from "../lib/stores/mail.svelte";
import { settings } from "../lib/stores/settings.svelte";

beforeEach(() => {
  mail.reset();
  settings.resetForTest();
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
    settings.setImagePolicy("ask");
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

  it("keeps remote images blocked without offering an override in block mode", async () => {
    settings.setImagePolicy("block");
    (mail as any).snapshot.selectedMessage = {
      id: "1", subject: null, from: { name: null, address: null },
      to: [], cc: [], bcc: [], date: null, body_text: null,
      body_html: '<img src="https://tracker.example/pixel.png">', attachments: [],
      account: { id: "1", name: null, address: null }, folders: [],
    };
    (mail as any).snapshot.bodyMode = "html";
    render(ReadingPane);
    await Promise.resolve();
    expect(screen.queryByRole("button", { name: /load images/i })).toBeNull();
    expect(screen.getByText(/remote images blocked/i)).toBeTruthy();
  });

  it("allows remote images automatically in allow mode", async () => {
    settings.setImagePolicy("allow");
    (mail as any).snapshot.selectedMessage = {
      id: "1", subject: null, from: { name: null, address: null },
      to: [], cc: [], bcc: [], date: null, body_text: null,
      body_html: '<img src="https://images.example/photo.jpg">', attachments: [],
      account: { id: "1", name: null, address: null }, folders: [],
    };
    (mail as any).snapshot.bodyMode = "html";
    const { container } = render(ReadingPane);
    await Promise.resolve();
    const srcdoc = container.querySelector("iframe")?.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("img-src * data:");
  });
});

describe("ReadingPane raw mode + HeaderUnfold + debug-gated DebugChunks", () => {
  beforeEach(() => {
    mail.reset();
    settings.resetForTest();
  });

  it("renders RawBodyView (Load button) when bodyMode=raw", async () => {
    (mail as any).snapshot.selectedMessage = {
      id: "42",
      subject: "Raw view",
      from: { name: null, address: "x@x" },
      to: [], cc: [], bcc: [], date: null,
      body_text: "plain only", body_html: null,
      attachments: [],
      account: { id: "1", name: null, address: null },
      folders: [],
    };
    (mail as any).snapshot.bodyMode = "raw";
    const { getByRole } = render(ReadingPane);
    await Promise.resolve();
    expect(getByRole("button", { name: /load raw bytes/i })).toBeTruthy();
  });

  it("mounts HeaderUnfold below the compact header", async () => {
    (mail as any).snapshot.selectedMessage = {
      id: "7",
      subject: "Hi",
      from: { name: "Anna", address: "anna@example.com" },
      to: [], cc: [], bcc: [], date: null,
      body_text: "body", body_html: null,
      attachments: [],
      account: { id: "1", name: "personal", address: null },
      folders: [],
    };
    (mail as any).snapshot.bodyMode = "plain";
    const { getByRole } = render(ReadingPane);
    await Promise.resolve();
    expect(getByRole("button", { name: /show full headers/i })).toBeTruthy();
  });

  it("renders DebugChunks only when settings.debug is true", async () => {
    (mail as any).snapshot.selectedMessage = {
      id: "9",
      subject: "Debug",
      from: { name: null, address: null },
      to: [], cc: [], bcc: [], date: null,
      body_text: "body", body_html: null,
      attachments: [],
      account: { id: "1", name: null, address: null },
      folders: [],
      matched_chunks: [{ kind: "body", text: "hit", score: 0.5 }],
    };
    (mail as any).snapshot.bodyMode = "plain";

    const offRender = render(ReadingPane);
    await Promise.resolve();
    expect(offRender.container.querySelector('[data-testid="debug-chunks-wrap"]')).toBeFalsy();
    offRender.unmount();

    settings.setDebug(true);
    const onRender = render(ReadingPane);
    await Promise.resolve();
    const wrap = onRender.container.querySelector('[data-testid="debug-chunks-wrap"]');
    expect(wrap).toBeTruthy();
    expect(onRender.container.querySelector("li")?.textContent).toContain("hit");
  });
});
