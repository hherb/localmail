import { describe, expect, it } from "vitest";
import {
  asRankable,
  asSortMode,
  displayedSort,
  RELEVANCE_UNAVAILABLE_REASON,
  relevanceUnavailable,
  sortClick,
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
  it("is true exactly when the server said the query cannot be ranked", () => {
    expect(relevanceUnavailable(false)).toBe(true);
  });

  it("is false when the server said it can be", () => {
    expect(relevanceUnavailable(true)).toBe(false);
  });

  it("is false while nothing has run, and for an older server", () => {
    // Unknown disables nothing: the same honest degradation the absent
    // `sort_applied` key already has.
    expect(relevanceUnavailable(null)).toBe(false);
  });

  it("no longer reads the REQUEST, which is what #353 was", () => {
    // The old rule was `applied === "date" && requested === "rank"`, so
    // recording a Date click flipped it and re-enabled Relevance on a query
    // that genuinely cannot be ranked. Rankability is a property of the
    // query, so the request cannot move it.
    expect(relevanceUnavailable(false)).toBe(true);
    // ...and it stays true however the user's preference then changes.
  });

  it("carries a reason that names the remedy", () => {
    expect(RELEVANCE_UNAVAILABLE_REASON).toMatch(/search text/i);
  });
});

describe("asRankable", () => {
  it("passes the two answers the server can give", () => {
    expect(asRankable(true)).toBe(true);
    expect(asRankable(false)).toBe(false);
  });

  it("reads an absent or null field as unknown", () => {
    expect(asRankable(undefined)).toBe(null);
    expect(asRankable(null)).toBe(null);
  });

  it("reads a NON-BOOLEAN as unknown rather than coercing it", () => {
    // `invoke<SearchResponse>` is an unchecked cast, so the value reaching
    // here is whatever the hop produced. Truthiness would read the string
    // "false" as rankable and the number 0 as not — both wrong, and the
    // second silently disables a working control.
    expect(asRankable("true")).toBe(null);
    expect(asRankable("false")).toBe(null);
    expect(asRankable(0)).toBe(null);
    expect(asRankable(1)).toBe(null);
    expect(asRankable({})).toBe(null);
  });
});

describe("sortClick", () => {
  it("records a click that disagrees with the stored preference", () => {
    // #353 itself: shown is `date` (what ran) while the preference is still
    // `rank`, so clicking the already-checked Date must be recorded even
    // though nothing on screen changes.
    expect(sortClick({ preference: "rank", shown: "date", clicked: "date" }))
      .toEqual({ record: true, resubmit: false });
  });

  it("does not re-run a search whose ordering would not change", () => {
    // The half that keeps the fix from costing a wasted round trip: the
    // rows are already date-ordered, so only the preference moves.
    expect(
      sortClick({ preference: "rank", shown: "date", clicked: "date" }).resubmit,
    ).toBe(false);
  });

  it("records and re-runs an ordinary change of mind", () => {
    expect(sortClick({ preference: "rank", shown: "rank", clicked: "date" }))
      .toEqual({ record: true, resubmit: true });
  });

  it("does nothing at all when the click agrees with both", () => {
    expect(sortClick({ preference: "date", shown: "date", clicked: "date" }))
      .toEqual({ record: false, resubmit: false });
    expect(sortClick({ preference: "rank", shown: "rank", clicked: "rank" }))
      .toEqual({ record: false, resubmit: false });
  });

  it("re-runs without recording when only the display disagrees", () => {
    // The transient between a click and its response landing: the
    // preference is already `date` and the rows on screen are still rank.
    expect(sortClick({ preference: "date", shown: "rank", clicked: "date" }))
      .toEqual({ record: false, resubmit: true });
  });

  it("asks two questions of two different fields, not one of one", () => {
    // The shape of the defect: one guard read one field and answered both
    // questions with it. Every combination where the two answers differ is
    // a case the old single guard got wrong.
    const modes = ["rank", "date"] as const;
    for (const preference of modes) {
      for (const shown of modes) {
        for (const clicked of modes) {
          const out = sortClick({ preference, shown, clicked });
          expect(out.record).toBe(preference !== clicked);
          expect(out.resubmit).toBe(shown !== clicked);
        }
      }
    }
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
