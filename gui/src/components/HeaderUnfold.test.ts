import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/svelte";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async () => {
    throw new Error("invoke should be mocked at the api wrapper level");
  }),
}));

import HeaderUnfold from "./HeaderUnfold.svelte";
import * as api from "../lib/api/full_headers";

describe("HeaderUnfold", () => {
  it("shows a 'Show full headers' button initially", () => {
    const { getByRole } = render(HeaderUnfold, { props: { messageId: "1" } });
    expect(getByRole("button", { name: /show full headers/i })).toBeTruthy();
  });

  it("fetches and renders headers on click", async () => {
    vi.spyOn(api, "getMessageFullHeaders").mockResolvedValue({
      id: "1",
      subject: null,
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
      headers: { "Message-Id": "<a@b>", "X-Spam-Status": "No" },
    });
    const { getByRole, findByText } = render(HeaderUnfold, { props: { messageId: "1" } });
    (getByRole("button", { name: /show full headers/i }) as HTMLButtonElement).click();
    expect(await findByText(/Message-Id/)).toBeTruthy();
    expect(await findByText(/X-Spam-Status/)).toBeTruthy();
  });
});
