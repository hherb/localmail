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
    expect(relevanceUnavailable(false, "rank", "date")).toBe(true);
  });

  it("is false when the server said it can be", () => {
    expect(relevanceUnavailable(true, "rank", "date")).toBe(false);
  });

  it("ignores the request whenever the server answered", () => {
    // #353 itself: the old rule was the inference below, so recording a Date
    // click flipped `requested` and re-enabled Relevance on a query that
    // genuinely cannot be ranked. Rankability is a property of the query, so
    // once the server has stated it no preference may move it — both
    // preferences, against both answers.
    for (const requested of ["rank", "date"] as const) {
      expect(relevanceUnavailable(false, requested, "date")).toBe(true);
      expect(relevanceUnavailable(true, requested, "date")).toBe(false);
    }
  });

  it("is false while nothing has run", () => {
    // Unknown with nothing applied claims nothing: the same honest
    // degradation the absent `sort_applied` key already has.
    expect(relevanceUnavailable(null, "rank", null)).toBe(false);
    expect(relevanceUnavailable(null, "date", null)).toBe(false);
  });

  it("falls back to the #345 inference for an INTERMEDIATE serve", () => {
    // A `serve` carrying #345 but not #353 — i.e. what a running daemon is
    // for the whole window between shipping this client and restarting it.
    // Reading `rankable === false` alone dropped the disable there, which is
    // #345 silently un-fixed; the inference is still proof, because
    // `statedSort` never sends `rank`, so a rank preference answered `date`
    // means the server found nothing to rank.
    expect(relevanceUnavailable(null, "rank", "date")).toBe(true);
  });

  it("claims nothing from the inference when the request was honoured", () => {
    // The other half of the fallback: a `date` request proves nothing either
    // way, and a `rank` answer proves rank was available.
    expect(relevanceUnavailable(null, "date", "date")).toBe(false);
    expect(relevanceUnavailable(null, "rank", "rank")).toBe(false);
    expect(relevanceUnavailable(null, "date", "rank")).toBe(false);
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
    // Not reachable through the component today — every state where the two
    // disagree leaves the clicked radio already checked or disabled — but the
    // two questions are independent and the rule must answer both.
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
