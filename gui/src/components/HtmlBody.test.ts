import { render } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import HtmlBody from "./HtmlBody.svelte";

describe("HtmlBody", () => {
  it("renders an iframe with sandbox attribute", () => {
    const { container } = render(HtmlBody, { props: { html: "<p>hi</p>", allowExternalImages: false } });
    const iframe = container.querySelector("iframe");
    expect(iframe).toBeTruthy();
    expect(iframe?.getAttribute("sandbox")).toBe("");  // empty sandbox = strictest
    expect(iframe?.getAttribute("scrolling")).toBe("yes");
  });

  it("srcdoc embeds a CSP meta tag", () => {
    const { container } = render(HtmlBody, { props: { html: "<p>hi</p>", allowExternalImages: false } });
    const iframe = container.querySelector("iframe");
    const srcdoc = iframe?.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("Content-Security-Policy");
    expect(srcdoc).toContain("default-src 'none'");
    expect(srcdoc).toContain("img-src 'self' data:");
  });

  it("srcdoc widens img-src when allowExternalImages=true", () => {
    const { container } = render(HtmlBody, { props: { html: "<p>hi</p>", allowExternalImages: true } });
    const srcdoc = container.querySelector("iframe")?.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("img-src *");
    expect(srcdoc).not.toContain("img-src 'self' data:");
  });

  it("forces the embedded document to remain vertically scrollable", () => {
    const { container } = render(HtmlBody, { props: { html: "<p>hi</p>", allowExternalImages: false } });
    const srcdoc = container.querySelector("iframe")?.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("overflow-y:auto!important");
    expect(srcdoc).toContain("overflow:visible!important");
  });

  it("includes the server-sanitised HTML payload in srcdoc body", () => {
    const html = "<p>hello <b>world</b></p>";
    const { container } = render(HtmlBody, { props: { html, allowExternalImages: false } });
    const srcdoc = container.querySelector("iframe")?.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain(html);
  });

  it("includes a literal </script> in the body without breaking rendering", () => {
    // We never have <script> inside srcdoc (no script-src), but defensively
    // accept that a server payload may contain the literal text and just check
    // it shows up in the srcdoc attribute.
    const html = "</script><img>";
    const { container } = render(HtmlBody, { props: { html, allowExternalImages: false } });
    const srcdoc = container.querySelector("iframe")?.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("</script>");
  });
});
