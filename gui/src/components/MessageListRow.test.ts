import { render, screen } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import MessageListRow from "./MessageListRow.svelte";
import type { MessageAccount, MessageAddress } from "../lib/tauri";

/**
 * The snippet is rendered as text, not as HTML (#391).
 *
 * The row used to render `{@html sanitizeSnippet(snippet)}`, an allowlist
 * that restored bare `<mark>`/`</mark>` through an otherwise-total escape,
 * described as "defense in depth against a sanitizer bypass on the server
 * side". There is no server-side sanitizer to bypass: `make_snippet` only
 * slices the chunk text and may add an ellipsis, and `<mark>` appears
 * nowhere in `src/` or in its history. So the allowlist could only ever
 * restore tags that came from the **message body**, rendering a quoted HTML
 * mail or a code snippet as a highlight instead of as what it says.
 *
 * It was never an XSS — the escape was total apart from that attribute-free
 * pair. These tests pin the fidelity fix and, with it, the absence of the
 * `{@html}` sink: a tag reaching the DOM as an element fails them.
 */

const BASE = {
  subject: "s",
  from: { name: "A", address: "a@b" } as MessageAddress,
  date: null,
  account: { id: "1", name: "acct" } as MessageAccount,
  selected: false,
  onSelect: () => {},
};

function renderSnippet(snippet: string): HTMLElement {
  const { container } = render(MessageListRow, { ...BASE, snippet });
  const el = container.querySelector(".snippet");
  if (!el) throw new Error("no .snippet element rendered");
  return el as HTMLElement;
}

describe("MessageListRow snippet", () => {
  it("renders plain text unchanged", () => {
    expect(renderSnippet("…leaves at 7:30 on Tue…").textContent)
      .toBe("…leaves at 7:30 on Tue…");
  });

  it("shows a literal <mark> from the body as text, not as a highlight", () => {
    const el = renderSnippet("use <mark>here</mark> to highlight");
    expect(el.textContent).toBe("use <mark>here</mark> to highlight");
    expect(el.querySelector("mark")).toBeNull();
  });

  it("puts no element into the DOM for markup in the body", () => {
    const el = renderSnippet("<script>alert(1)</script><b>x</b>");
    expect(el.textContent).toBe("<script>alert(1)</script><b>x</b>");
    expect(el.children.length).toBe(0);
  });

  it("renders no snippet element at all when there is none", () => {
    const { container } = render(MessageListRow, { ...BASE, snippet: null });
    expect(container.querySelector(".snippet")).toBeNull();
  });

  it("still renders the rest of the row", () => {
    render(MessageListRow, { ...BASE, snippet: "hi" });
    expect(screen.getByText("s")).toBeTruthy();
  });
});
