import { describe, it, expect } from "vitest";
import { formatError } from "./format_error";

describe("formatError", () => {
  it("returns plain strings unchanged", () => {
    expect(formatError("boom")).toBe("boom");
  });

  it("renders simple {kind, detail:string}", () => {
    expect(formatError({ kind: "Auth", detail: "NotLoggedIn" })).toBe(
      "Auth: NotLoggedIn",
    );
  });

  it("renders nested {kind, detail:{kind, detail}} wrappers", () => {
    expect(
      formatError({
        kind: "Http",
        detail: { kind: "Network", detail: "connection refused" },
      }),
    ).toBe("Http: Network: connection refused");
  });

  it("extracts title + detail from an RFC 7807 problem body", () => {
    const problem = JSON.stringify({
      type: "/problems/feature-unavailable",
      title: "Feature unavailable",
      status: 503,
      detail: "search not configured on this server",
    });
    const msg = formatError({
      kind: "Http",
      detail: {
        kind: "HttpStatus",
        detail: { status: 503, body: problem },
      },
    });
    expect(msg).toBe(
      "Http: 503 Feature unavailable: search not configured on this server",
    );
  });

  it("falls back to status + raw body when body is not JSON", () => {
    const msg = formatError({
      kind: "HttpStatus",
      detail: { status: 502, body: "Bad Gateway" },
    });
    expect(msg).toBe("502: Bad Gateway");
  });

  it("never emits [object Object] for nested unstructured objects", () => {
    const msg = formatError({ kind: "Weird", detail: { foo: 1, bar: "x" } });
    expect(msg).not.toContain("[object Object]");
    expect(msg).toBe('Weird: {"foo":1,"bar":"x"}');
  });

  it("returns kind alone when detail is missing", () => {
    expect(formatError({ kind: "CertMismatch" })).toBe("CertMismatch");
  });

  // Issue #22 — AttachmentError + RawMessageError replaced AuthError::Io.
  // The new wire shapes still walk through the generic {kind, detail}
  // recursion, but exercise the variants explicitly so a future Rust-side
  // rename of any of them lands a failing test here.
  describe("AttachmentError (issue #22)", () => {
    it("renders InvalidSha256 with the offending input", () => {
      expect(
        formatError({ kind: "InvalidSha256", detail: "not-a-sha" }),
      ).toBe("InvalidSha256: not-a-sha");
    });

    it("renders TooLarge with structured size + max", () => {
      const msg = formatError({
        kind: "TooLarge",
        detail: { size: 209715200, max: 104857600 },
      });
      // Structured detail falls through safeStringify; we just lock the
      // numbers into the rendered string so a UI reader can see them.
      expect(msg).toContain("TooLarge");
      expect(msg).toContain("209715200");
      expect(msg).toContain("104857600");
    });

    it("renders Http with the bare status code", () => {
      expect(formatError({ kind: "Http", detail: 403 })).toBe("Http: 403");
    });

    it("renders Auth pre-check failures via the nested AuthError wrapper", () => {
      // AttachmentError::Auth(AuthError::NotConnected) → nested {kind} chain.
      // kind-alone branch fires at the inner level (no detail), so the outer
      // wrapper's branch returns "<kind>: <kind>".
      expect(
        formatError({ kind: "Auth", detail: { kind: "NotConnected" } }),
      ).toBe("Auth: NotConnected");
    });

    it("renders Write with structured path + error", () => {
      const msg = formatError({
        kind: "Write",
        detail: { path: "/tmp/x", error: "permission denied" },
      });
      expect(msg).toContain("Write");
      expect(msg).toContain("/tmp/x");
      expect(msg).toContain("permission denied");
    });
  });
});
