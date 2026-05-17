import { fireEvent, render } from "@testing-library/svelte";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("../lib/tauri", () => ({
  fetchAttachmentBytes: vi.fn(async () => ({
    bytes: Array.from(new Uint8Array([137, 80, 78, 71])), // PNG magic bytes
    content_type: "image/png",
  })),
}));

import AttachmentPreviewModal from "./AttachmentPreviewModal.svelte";

// jsdom doesn't implement URL.createObjectURL; provide a stub so the
// component's blobUrl assignment succeeds and the <img> branch is reached.
beforeAll(() => {
  if (!URL.createObjectURL) {
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
  }
});

afterEach(() => { vi.clearAllMocks(); });

describe("AttachmentPreviewModal", () => {
  it("renders an <img> for image content_type", async () => {
    const { container } = render(AttachmentPreviewModal, {
      props: { sha256: "abc", filename: "photo.png", onClose: () => {} },
    });
    // Wait for the async fetchAttachmentBytes microtask + Svelte DOM flush.
    await new Promise((r) => setTimeout(r, 50));
    expect(container.querySelector("img")).toBeTruthy();
  });

  it("calls onClose when backdrop is clicked", async () => {
    const onClose = vi.fn();
    const { container } = render(AttachmentPreviewModal, {
      props: { sha256: "abc", filename: "photo.png", onClose },
    });
    await fireEvent.click(container.querySelector(".backdrop")!);
    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose on Escape", async () => {
    const onClose = vi.fn();
    render(AttachmentPreviewModal, {
      props: { sha256: "abc", filename: "photo.png", onClose },
    });
    await fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});
