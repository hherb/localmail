import { describe, expect, it } from "vitest";

import { isSearchCursorExpired } from "./search_cursor_expired";

describe("isSearchCursorExpired", () => {
  it("matches when error message contains the problem type", () => {
    expect(isSearchCursorExpired(
      new Error('{"type":"/problems/search-cursor-expired"}'),
    )).toBe(true);
  });
  it("matches plain strings", () => {
    expect(isSearchCursorExpired(
      "server returned 409 /problems/search-cursor-expired",
    )).toBe(true);
  });
  it("returns false for unrelated errors", () => {
    expect(isSearchCursorExpired(new Error("network unreachable"))).toBe(false);
  });

  // Tauri serialises AuthError::Http(HttpError::HttpStatus { status, body })
  // as nested {kind, detail} objects. The predicate must walk these — there
  // is no .message field, so the old String(err) fallback gave "[object Object]".
  it("matches real Tauri-shaped 409 error object", () => {
    const tauriErr = {
      kind: "Http",
      detail: {
        kind: "HttpStatus",
        detail: {
          status: 409,
          body: '{"type":"/problems/search-cursor-expired","title":"Search cursor expired","status":409}',
        },
      },
    };
    expect(isSearchCursorExpired(tauriErr)).toBe(true);
  });

  it("returns false for Tauri-shaped non-409 error", () => {
    const tauriErr = {
      kind: "Http",
      detail: {
        kind: "HttpStatus",
        detail: { status: 500, body: '{"type":"/problems/internal-error"}' },
      },
    };
    expect(isSearchCursorExpired(tauriErr)).toBe(false);
  });

  it("returns false for plain object with no problem type", () => {
    expect(isSearchCursorExpired({ kind: "NotConnected" })).toBe(false);
  });

  it("returns false for null and undefined", () => {
    expect(isSearchCursorExpired(null)).toBe(false);
    expect(isSearchCursorExpired(undefined)).toBe(false);
  });
});
