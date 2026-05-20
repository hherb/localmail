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
});
