import { fireEvent, render } from "@testing-library/svelte";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("../lib/tauri", () => ({
  fetchAttachmentBytes: vi.fn(async () => ({
    bytes: Array.from(new Uint8Array([137, 80, 78, 71])), // PNG magic bytes
    content_type: "image/png",
  })),
}));

// Mock the dynamic pdfjs imports the component does. We expose a hoisted
// `numPages` knob so individual tests can vary the page count.
const pdfState = vi.hoisted(() => ({ numPages: 3 }));

vi.mock("pdfjs-dist", () => {
  const makePage = () => ({
    getViewport: ({ scale }: { scale: number }) => ({ width: 100 * scale, height: 100 * scale }),
    render: () => ({ promise: Promise.resolve() }),
  });
  return {
    getDocument: vi.fn(() => ({
      promise: Promise.resolve({
        get numPages() { return pdfState.numPages; },
        getPage: vi.fn(async () => makePage()),
      }),
    })),
    GlobalWorkerOptions: { workerSrc: "" },
  };
});

vi.mock("pdfjs-dist/build/pdf.worker.mjs?url", () => ({ default: "worker.js" }));

import AttachmentPreviewModal from "./AttachmentPreviewModal.svelte";
import { fetchAttachmentBytes } from "../lib/tauri";

const mockFetch = vi.mocked(fetchAttachmentBytes);

// jsdom doesn't implement URL.createObjectURL; provide a stub so the
// component's blobUrl assignment succeeds and the <img> branch is reached.
beforeAll(() => {
  if (!URL.createObjectURL) {
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
  }
});

afterEach(() => {
  vi.clearAllMocks();
  pdfState.numPages = 3;
  // Default to PNG mock for image tests; PDF tests override before render().
  mockFetch.mockImplementation(async () => ({
    bytes: Array.from(new Uint8Array([137, 80, 78, 71])),
    content_type: "image/png",
  }));
});

function mockPdfBytes(): void {
  mockFetch.mockImplementation(async () => ({
    bytes: Array.from(new Uint8Array([37, 80, 68, 70])), // %PDF
    content_type: "application/pdf",
  }));
}

async function flush(): Promise<void> {
  // Two macrotask ticks: one for fetchAttachmentBytes + dynamic imports,
  // one for the post-import getDocument().promise + first renderPage().
  await new Promise((r) => setTimeout(r, 50));
  await new Promise((r) => setTimeout(r, 50));
}

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

  it("renders Page X of Y counter for a multi-page PDF", async () => {
    mockPdfBytes();
    pdfState.numPages = 3;
    const { container } = render(AttachmentPreviewModal, {
      props: { sha256: "abc", filename: "doc.pdf", onClose: () => {} },
    });
    await flush();
    const counter = container.querySelector(".page-counter");
    expect(counter).toBeTruthy();
    expect(counter!.textContent).toBe("Page 1 of 3");
  });

  it("disables Prev on page 1 and Next on the last page", async () => {
    mockPdfBytes();
    pdfState.numPages = 3;
    const { container } = render(AttachmentPreviewModal, {
      props: { sha256: "abc", filename: "doc.pdf", onClose: () => {} },
    });
    await flush();
    const prev = container.querySelector('button[aria-label="Previous page"]') as HTMLButtonElement;
    const next = container.querySelector('button[aria-label="Next page"]') as HTMLButtonElement;
    expect(prev).toBeTruthy();
    expect(next).toBeTruthy();
    expect(prev.disabled).toBe(true);
    expect(next.disabled).toBe(false);

    await fireEvent.click(next);
    await flush();
    expect(prev.disabled).toBe(false);
    expect(next.disabled).toBe(false);
    expect(container.querySelector(".page-counter")!.textContent).toBe("Page 2 of 3");

    await fireEvent.click(next);
    await flush();
    expect(prev.disabled).toBe(false);
    expect(next.disabled).toBe(true);
    expect(container.querySelector(".page-counter")!.textContent).toBe("Page 3 of 3");
  });

  it("hides controls for a single-page PDF only when pageCount is 1", async () => {
    mockPdfBytes();
    pdfState.numPages = 1;
    const { container } = render(AttachmentPreviewModal, {
      props: { sha256: "abc", filename: "one.pdf", onClose: () => {} },
    });
    await flush();
    // Counter still renders; both nav buttons are disabled at the only page.
    const counter = container.querySelector(".page-counter");
    expect(counter!.textContent).toBe("Page 1 of 1");
    const prev = container.querySelector('button[aria-label="Previous page"]') as HTMLButtonElement;
    const next = container.querySelector('button[aria-label="Next page"]') as HTMLButtonElement;
    expect(prev.disabled).toBe(true);
    expect(next.disabled).toBe(true);
  });
});
