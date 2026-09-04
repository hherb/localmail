import { describe, expect, it } from "vitest";
import { isCursorRejected, statedSort } from "./search_paging";

describe("statedSort", () => {
  it("states the sort on a fresh request, which has no cursor to inherit one from", () => {
    expect(statedSort(null, "date", "invoice")).toBe("date");
    expect(statedSort(null, "rank", "invoice")).toBe("rank");
  });

  it("omits the sort when a cursor is present, because the cursor carries the ordering", () => {
    expect(statedSort("K|abc", "date", "invoice")).toBeUndefined();
    expect(statedSort("tok:2", "rank", "invoice")).toBeUndefined();
  });

  it("omits it even when the stated sort agrees with the cursor", () => {
    // Agreement is not the point: the store's sort can change under a live
    // cursor, so a request that never states one cannot contradict it.
    expect(statedSort("tok:2", "date", "invoice")).toBeUndefined();
  });

  // #324: the server refuses sort="rank" for a query it cannot rank, and a
  // blank search box with a filter chip set is an ordinary, shipped flow —
  // "everything from this account, with attachments". Stating the store's
  // default "rank" there would turn that flow into an error banner. Omitting
  // is not a fallback: the server resolves an unstated sort to the branch
  // that will actually serve the request, which for a textless query is the
  // date walk — exactly what this flow received before #324.
  it("omits a rank sort when the query has nothing to rank", () => {
    expect(statedSort(null, "rank", "")).toBeUndefined();
    expect(statedSort(null, "rank", "   ")).toBeUndefined();
  });

  it("still states a date sort on a textless query", () => {
    // "date" is what the server will serve anyway, so stating it is honest
    // and survives being echoed back — and dropping it would make the
    // store's sort selector inert for the blank-box case.
    expect(statedSort(null, "date", "")).toBe("date");
  });

  it("states rank as soon as there is anything to rank", () => {
    // The positive control: a rule matching too broadly would silently
    // retire the sort selector for every search.
    expect(statedSort(null, "rank", "a")).toBe("rank");
    expect(statedSort(null, "rank", "  invoice  ")).toBe("rank");
  });

  // Known imprecision, and deliberate. The server lifts filter operators out
  // before deciding, so `subject:invoice` reads as textless there and as
  // text here. Matching that would mean re-implementing `parse_query` in the
  // client — a second parser to keep in step with the first — for the cost of
  // one loud, actionable 400 on a query the user typed operators into. The
  // cheap half is the one that matters: the blank box is what the GUI reaches
  // by itself.
  it("does not try to reproduce the server's operator parsing", () => {
    expect(statedSort(null, "rank", "subject:invoice")).toBe("rank");
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
