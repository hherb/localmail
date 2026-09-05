import { describe, expect, it } from "vitest";
import {
  asSortMode,
  displayedSort,
  RELEVANCE_UNAVAILABLE_REASON,
  relevanceUnavailable,
} from "./sort_display";

describe("displayedSort", () => {
  it("shows the stored preference before anything has run", () => {
    expect(displayedSort("rank", null)).toBe("rank");
    expect(displayedSort("date", null)).toBe("date");
  });

  it("shows the ordering that ran, not the one requested", () => {
    // #345 itself: the request meant rank, the server served date.
    expect(displayedSort("rank", "date")).toBe("date");
  });

  it("agrees with the request when the server honoured it", () => {
    expect(displayedSort("rank", "rank")).toBe("rank");
    expect(displayedSort("date", "date")).toBe("date");
  });

  it("still shows what ran when a NEWER request disagrees", () => {
    // The sixth input, and the only one that separates the two radio
    // bindings: with `applied` and `requested` disagreeing the *other* way,
    // a binding that reads the request renders neither radio checked.
    // Reachable as the transient between clicking Date and its response
    // landing, which the component really does render.
    expect(displayedSort("date", "rank")).toBe("rank");
  });
});

describe("relevanceUnavailable", () => {
  it("is true only when the server resolved date for a rank request", () => {
    expect(relevanceUnavailable("rank", "date")).toBe(true);
  });

  it("is false while nothing has run", () => {
    expect(relevanceUnavailable("rank", null)).toBe(false);
    expect(relevanceUnavailable("date", null)).toBe(false);
  });

  it("is false when rank ran", () => {
    expect(relevanceUnavailable("rank", "rank")).toBe(false);
  });

  it("is false for an explicit date request", () => {
    // A date request proves nothing about whether rank was available, so it
    // must never disable the control. This is the deliberate imprecision:
    // one click on Relevance turns the case into ("rank", "date") above.
    expect(relevanceUnavailable("date", "date")).toBe(false);
  });

  it("carries a reason that names the remedy", () => {
    expect(RELEVANCE_UNAVAILABLE_REASON).toMatch(/search text/i);
  });
});

describe("asSortMode", () => {
  it("passes the two orderings the server can report", () => {
    expect(asSortMode("rank")).toBe("rank");
    expect(asSortMode("date")).toBe("date");
  });

  it("reads an absent or null field as unknown", () => {
    // Absent is an older `serve`; null is what the Tauri hop actually
    // sends for one, since `Option<String>` has no skip_serializing_if.
    expect(asSortMode(undefined)).toBe(null);
    expect(asSortMode(null)).toBe(null);
  });

  it("reads an UNRECOGNISED ordering as unknown, not as itself", () => {
    // The gap this closes: Rust accepts any string and `invoke<T>` is an
    // unchecked cast, so without this a newer server's third ordering
    // reaches the selector and matches neither radio — leaving the control
    // with nothing checked. Degrading to "unknown" shows the request.
    expect(asSortMode("relevance_then_date")).toBe(null);
    expect(asSortMode("Date")).toBe(null);
    expect(asSortMode(7)).toBe(null);
    expect(asSortMode({})).toBe(null);
  });
});
