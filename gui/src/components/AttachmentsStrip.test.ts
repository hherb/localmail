import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import AttachmentsStrip from "./AttachmentsStrip.svelte";
import { mail } from "../lib/stores/mail.svelte";

vi.mock("@tauri-apps/plugin-dialog", () => ({
  save: vi.fn(async () => "/tmp/x.pdf"),
}));
vi.mock("../lib/tauri", () => ({
  downloadAttachment: vi.fn(async () => ({ bytes_written: 1234, path: "/tmp/x.pdf" })),
}));

afterEach(() => { mail.reset(); vi.clearAllMocks(); });

describe("AttachmentsStrip", () => {
  it("renders nothing when selectedMessage has no attachments", () => {
    const { container } = render(AttachmentsStrip);
    expect(container.querySelectorAll(".attachment").length).toBe(0);
  });

  it("renders one row per attachment with download button", () => {
    (mail as any).snapshot.selectedMessage = {
      id: "1", attachments: [
        { filename: "invoice.pdf", sha256: "deadbeef" },
        { filename: "photo.jpg", sha256: "feedface" },
      ],
      body_text: null, body_html: null, subject: null,
      from: { name: null, address: null }, to: [], cc: [], bcc: [], date: null,
      account: { id: "1", name: null, address: null }, folders: [],
    };
    render(AttachmentsStrip);
    expect(screen.getByText("invoice.pdf")).toBeTruthy();
    expect(screen.getByText("photo.jpg")).toBeTruthy();
  });

  it("clicking download opens save dialog and calls downloadAttachment", async () => {
    (mail as any).snapshot.selectedMessage = {
      id: "1", attachments: [{ filename: "invoice.pdf", sha256: "deadbeef" }],
      body_text: null, body_html: null, subject: null,
      from: { name: null, address: null }, to: [], cc: [], bcc: [], date: null,
      account: { id: "1", name: null, address: null }, folders: [],
    };
    render(AttachmentsStrip);
    await fireEvent.click(screen.getByRole("button", { name: /download/i }));
    const { save } = await import("@tauri-apps/plugin-dialog");
    const { downloadAttachment } = await import("../lib/tauri");
    expect(save).toHaveBeenCalled();
    expect(downloadAttachment).toHaveBeenCalledWith("deadbeef", "/tmp/x.pdf");
  });
});
