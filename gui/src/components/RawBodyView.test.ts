import { fireEvent, render } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

import RawBodyView from "./RawBodyView.svelte";

afterEach(() => {
  invokeMock.mockReset();
});

function bytesForRawMessage(raw: string): number[] {
  return Array.from(new TextEncoder().encode(raw));
}

const PLAIN_UTF8 = "From: a@b\r\nSubject: hi\r\n\r\nBody";

describe("RawBodyView", () => {
  it("shows a Load button initially, fetches on click, then renders the decoded body", async () => {
    invokeMock.mockResolvedValue(bytesForRawMessage(PLAIN_UTF8));

    const { getByRole, findByText } = render(RawBodyView, { props: { messageId: "42" } });
    const btn = getByRole("button", { name: /load/i });
    await fireEvent.click(btn);
    const node = await findByText(/From: a@b/);
    expect(node).toBeTruthy();
  });

  it("shows an error when the fetch fails", async () => {
    invokeMock.mockRejectedValue(new Error("nope"));
    const { getByRole, findByText } = render(RawBodyView, { props: { messageId: "42" } });
    await fireEvent.click(getByRole("button", { name: /load/i }));
    const err = await findByText(/nope/);
    expect(err).toBeTruthy();
  });

  it("renders the encoding dropdown once bytes are loaded", async () => {
    invokeMock.mockResolvedValue(bytesForRawMessage(PLAIN_UTF8));
    const { getByRole, findByLabelText } = render(RawBodyView, { props: { messageId: "42" } });
    await fireEvent.click(getByRole("button", { name: /load/i }));

    const select = await findByLabelText(/text encoding/i);
    expect(select).toBeTruthy();
    expect((select as HTMLSelectElement).value).toBe("auto");
  });

  it("shows a detected-charset hint when AUTO sniffs a header charset", async () => {
    const raw = "Content-Type: text/plain; charset=iso-8859-1\r\n\r\nbody";
    invokeMock.mockResolvedValue(bytesForRawMessage(raw));

    const { getByRole, findByTestId } = render(RawBodyView, { props: { messageId: "42" } });
    await fireEvent.click(getByRole("button", { name: /load/i }));

    const hint = await findByTestId("charset-detected");
    expect(hint.textContent).toMatch(/iso-8859-1/);
  });

  it("does not show a detected-charset hint when no charset is declared", async () => {
    invokeMock.mockResolvedValue(bytesForRawMessage(PLAIN_UTF8));
    const { getByRole, queryByTestId, findByRole } = render(RawBodyView, {
      props: { messageId: "42" },
    });
    await fireEvent.click(getByRole("button", { name: /load/i }));
    await findByRole("combobox", { name: /text encoding/i });

    expect(queryByTestId("charset-detected")).toBeNull();
  });

  it("hint shows the canonical label and decodes cleanly when the message declares a non-canonical alias (latin-1)", async () => {
    // Real-world charset value the WebView's TextDecoder doesn't recognise
    // verbatim; we expect the helper to canonicalise to iso-8859-1 so the
    // hint matches what's actually being used and the body decodes cleanly.
    const header = "Content-Type: text/plain; charset=latin-1\r\n\r\n";
    const headerBytes = Array.from(new TextEncoder().encode(header));
    const bodyLatin1 = [0x63, 0x61, 0x66, 0xe9];
    invokeMock.mockResolvedValue([...headerBytes, ...bodyLatin1]);

    const { getByRole, findByTestId, container } = render(RawBodyView, {
      props: { messageId: "42" },
    });
    await fireEvent.click(getByRole("button", { name: /load/i }));

    const hint = await findByTestId("charset-detected");
    expect(hint.textContent).toMatch(/iso-8859-1/);
    expect(hint.textContent).not.toMatch(/latin-1/);

    const pre = container.querySelector("pre.raw");
    expect(pre!.textContent).toContain("café");
  });

  it("re-decodes the body live when the encoding dropdown changes", async () => {
    // Body bytes 0x63 0x61 0x66 0xe9 = "café" in Latin-1, but invalid UTF-8.
    // Header + CRLFCRLF + Latin-1 body bytes.
    const header = "Content-Type: text/plain\r\n\r\n";
    const headerBytes = Array.from(new TextEncoder().encode(header));
    const bodyLatin1 = [0x63, 0x61, 0x66, 0xe9];
    invokeMock.mockResolvedValue([...headerBytes, ...bodyLatin1]);

    const { getByRole, findByLabelText, container } = render(RawBodyView, {
      props: { messageId: "42" },
    });
    await fireEvent.click(getByRole("button", { name: /load/i }));

    const select = (await findByLabelText(/text encoding/i)) as HTMLSelectElement;
    const pre = container.querySelector("pre.raw");
    expect(pre).toBeTruthy();
    // Default (auto, no charset header → utf-8) replaces 0xe9 with U+FFFD.
    expect(pre!.textContent).toContain("caf�");

    await fireEvent.change(select, { target: { value: "iso-8859-1" } });
    expect(pre!.textContent).toContain("café");
  });
});
