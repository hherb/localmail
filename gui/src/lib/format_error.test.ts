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
});
