import { describe, it, expect } from "vitest";
import {
  clampPaneWidths,
  parseStoredWidths,
  serializeWidths,
  DEFAULT_LEFT_WIDTH_PX,
  DEFAULT_MIDDLE_WIDTH_PX,
  MIN_PANE_WIDTH_PX,
} from "./splitter";

describe("clampPaneWidths", () => {
  it("returns defaults when total < 3 * MIN", () => {
    const out = clampPaneWidths(
      { left: 220, middle: 340 },
      { containerWidth: MIN_PANE_WIDTH_PX * 2 },
    );
    expect(out.left).toBe(DEFAULT_LEFT_WIDTH_PX);
    expect(out.middle).toBe(DEFAULT_MIDDLE_WIDTH_PX);
  });

  it("clamps left to >= MIN", () => {
    const out = clampPaneWidths({ left: 10, middle: 340 }, { containerWidth: 1200 });
    expect(out.left).toBeGreaterThanOrEqual(MIN_PANE_WIDTH_PX);
  });

  it("clamps middle so that right pane is >= MIN", () => {
    const out = clampPaneWidths({ left: 200, middle: 9000 }, { containerWidth: 1200 });
    expect(1200 - out.left - out.middle).toBeGreaterThanOrEqual(MIN_PANE_WIDTH_PX);
  });

  it("passes valid widths through unchanged", () => {
    const out = clampPaneWidths({ left: 240, middle: 360 }, { containerWidth: 1200 });
    expect(out.left).toBe(240);
    expect(out.middle).toBe(360);
  });
});

describe("parseStoredWidths / serializeWidths round-trip", () => {
  it("serializes and deserializes a config", () => {
    const cfg = { left: 240, middle: 360 };
    const s = serializeWidths(cfg);
    expect(parseStoredWidths(s)).toEqual(cfg);
  });

  it("returns null on invalid JSON", () => {
    expect(parseStoredWidths("not json")).toBeNull();
  });

  it("returns null on missing keys", () => {
    expect(parseStoredWidths(JSON.stringify({ left: 100 }))).toBeNull();
  });

  it("returns null on non-finite numbers", () => {
    expect(parseStoredWidths(JSON.stringify({ left: NaN, middle: 100 }))).toBeNull();
    expect(parseStoredWidths(JSON.stringify({ left: 100, middle: Infinity }))).toBeNull();
  });
});
