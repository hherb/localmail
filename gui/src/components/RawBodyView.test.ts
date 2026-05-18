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

describe("RawBodyView", () => {
  it("shows a Load button initially, fetches on click, then renders the decoded body", async () => {
    const enc = new TextEncoder();
    const bytes = enc.encode("From: a@b\r\nSubject: hi\r\n\r\nBody");
    invokeMock.mockResolvedValue(Array.from(bytes));

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
});
