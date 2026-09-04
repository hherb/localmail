import { describe, expect, it } from "vitest";
import { isCursorRejected, statedSort } from "./search_paging";

describe("statedSort", () => {
  it("states a date sort on a fresh request, which has no cursor to inherit one from", () => {
    expect(statedSort(null, "date")).toBe("date");
  });

  it("omits the sort when a cursor is present, because the cursor carries the ordering", () => {
    expect(statedSort("K|abc", "date")).toBeUndefined();
    expect(statedSort("tok:2", "rank")).toBeUndefined();
  });

  it("omits it even when the stated sort agrees with the cursor", () => {
    // Agreement is not the point: the store's sort can change under a live
    // cursor, so a request that never states one cannot contradict it.
    expect(statedSort("tok:2", "date")).toBeUndefined();
  });

  // #324. The server refuses `sort="rank"` for a query it cannot rank, and it
  // decides that *after* lifting filter operators out — so `from:alice` and
  // `has:attachment`, both advertised by SearchBar's own placeholder, are
  // textless server-side. Rather than reproduce `parse_query` here (a second
  // parser to keep in step with the first) or test the raw box naively (which
  // turns those advertised searches into an error banner), `rank` is simply
  // never stated. Omitting is exactly equivalent: the server resolves an
  // unstated sort to the branch that will serve the request, which is `rank`
  // whenever ranking is possible at all.
  it("never states rank", () => {
    expect(statedSort(null, "rank")).toBeUndefined();
  });

  it("still states date, which the server serves for any query", () => {
    // The positive control: a rule that dropped every sort would silently
    // retire the sort selector, and every assertion above would still pass.
    expect(statedSort(null, "date")).toBe("date");
  });
});

describe("isCursorRejected", () => {
  const nested = (status: number) => ({
    kind: "Http",
    detail: { kind: "HttpStatus", detail: { status, body: "{}" } },
  });

  it("is true for the 400 the server answers an unusable cursor with", () => {
    expect(isCursorRejected(nested(400))).toBe(true);
  });

  it("is false for the 409 that has its own transparent recovery", () => {
    expect(isCursorRejected(nested(409))).toBe(false);
  });

  it("is false for a server-side failure, which retrying can legitimately clear", () => {
    expect(isCursorRejected(nested(500))).toBe(false);
    expect(isCursorRejected(nested(503))).toBe(false);
  });

  it("is false for an error carrying no HTTP status at all", () => {
    expect(isCursorRejected(new Error("offline"))).toBe(false);
    expect(isCursorRejected(null)).toBe(false);
    expect(isCursorRejected("boom")).toBe(false);
  });
});
