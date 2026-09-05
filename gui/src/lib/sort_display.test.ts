import { describe, expect, it } from "vitest";
import {
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
