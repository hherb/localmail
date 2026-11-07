import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-dialog", () => ({
  save: vi.fn(async () => "/tmp/report.pdf"),
}));
vi.mock("../lib/tauri", () => ({
  downloadAttachment: vi.fn(async () => {
    // What the command really rejects with (#464).
    throw { kind: "Timeout", detail: { seconds: 15 } };
  }),
}));

import AttachmentRow from "./AttachmentRow.svelte";

describe("AttachmentRow", () => {
  it("renders a download error, not [object Object]", async () => {
    const { getByRole, findByText } = render(AttachmentRow, {
      props: { filename: "report.pdf", sha256: "a".repeat(64) },
    });
    await fireEvent.click(getByRole("button", { name: /download/i }));
    expect(await findByText('Timeout: {"seconds":15}')).toBeTruthy();
  });
});
