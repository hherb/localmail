import { describe, expect, it } from "vitest";
import { sanitizeSnippet } from "./snippet_sanitize";

describe("sanitizeSnippet", () => {
  it("passes plain text through unchanged (escaping HTML chars)", () => {
    expect(sanitizeSnippet("hello world")).toBe("hello world");
  });

  it("escapes < and > and & that aren't part of allowed tags", () => {
    expect(sanitizeSnippet("a < b & c > d")).toBe("a &lt; b &amp; c &gt; d");
  });

  it("preserves <mark> and </mark>", () => {
    expect(sanitizeSnippet("see <mark>here</mark>")).toBe("see <mark>here</mark>");
  });

  it("strips disallowed tags like <script>", () => {
    expect(sanitizeSnippet("<script>alert(1)</script>")).toBe("&lt;script&gt;alert(1)&lt;/script&gt;");
  });

  it("strips <mark> with attributes", () => {
    // We only allow bare <mark>; an attribute-bearing one is escaped.
    const out = sanitizeSnippet('<mark style="x">hi</mark>');
    expect(out).toBe("&lt;mark style=&quot;x&quot;&gt;hi&lt;/mark&gt;");
  });

  it("handles null/empty input", () => {
    expect(sanitizeSnippet(null)).toBe("");
    expect(sanitizeSnippet("")).toBe("");
  });

  it("preserves text around <mark>", () => {
    expect(sanitizeSnippet("…leaves at <mark>7:30</mark> on Tue…"))
      .toBe("…leaves at <mark>7:30</mark> on Tue…");
  });

  it("does not let placeholder-string text smuggle <mark> tags through", () => {
    // The implementation uses LOCALMAIL_MARK_OPEN_<nonce> internally; the
    // literal prefix in adversarial input must not be restored to <mark>.
    const out = sanitizeSnippet("LOCALMAIL_MARK_OPEN evil");
    expect(out).not.toContain("<mark>");
    expect(out).toContain("LOCALMAIL_MARK_OPEN");
  });

  it("escapes excess unpaired </mark>", () => {
    // One open, two closes: the second </mark> must fall through to escaping.
    const out = sanitizeSnippet("<mark>a</mark></mark>");
    expect(out).toBe("<mark>a</mark>&lt;/mark&gt;");
  });
});
