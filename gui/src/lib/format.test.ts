import { describe, expect, it } from "vitest";
import { addressLabel, formatRelativeDate, truncate } from "./format";
import type { MessageAddress } from "./tauri";

describe("addressLabel", () => {
  it("prefers name over address", () => {
    const a: MessageAddress = { name: "Anna H.", address: "anna@example.com" };
    expect(addressLabel(a)).toBe("Anna H.");
  });

  it("falls back to address when name is null", () => {
    const a: MessageAddress = { name: null, address: "anna@example.com" };
    expect(addressLabel(a)).toBe("anna@example.com");
  });

  it("returns placeholder when both are null", () => {
    const a: MessageAddress = { name: null, address: null };
    expect(addressLabel(a)).toBe("(unknown sender)");
  });
});

describe("truncate", () => {
  it("returns input unchanged if shorter than limit", () => {
    expect(truncate("hello", 10)).toBe("hello");
  });

  it("truncates and appends ellipsis when longer", () => {
    expect(truncate("hello world", 5)).toBe("hello…");
  });

  it("handles null/undefined as empty string", () => {
    expect(truncate(null, 10)).toBe("");
    expect(truncate(undefined, 10)).toBe("");
  });
});

describe("formatRelativeDate", () => {
  it("returns empty string for null", () => {
    expect(formatRelativeDate(null, new Date("2026-05-17T12:00:00Z"))).toBe("");
  });

  it("returns time only for same day", () => {
    // sameDay compares LOCAL calendar fields; constructing with
    // `Date(y, m, d, h, m)` pins both args to the same local day in any TZ.
    const now = new Date(2026, 4, 17, 15, 0);
    const earlier = new Date(2026, 4, 17, 9, 30);
    const out = formatRelativeDate(earlier.toISOString(), now);
    // Time format is locale-dependent; just assert it contains a digit and a colon.
    expect(out).toMatch(/\d+:\d+/);
  });

  it("returns short date for earlier in the same year", () => {
    const out = formatRelativeDate(
      "2026-03-03T08:14:00Z",
      new Date("2026-05-17T12:00:00Z"),
    );
    // Locale-dependent ordering: en-US → "Mar 3", en-GB/en-AU → "3 Mar".
    // Assert a 3-letter month token sits next to the day, in either order.
    expect(out.toLowerCase()).toMatch(/[a-z]{3}\s+\d+|\d+\s+[a-z]{3}/);
  });

  it("returns full date for older messages", () => {
    const out = formatRelativeDate(
      "2024-12-25T08:14:00Z",
      new Date("2026-05-17T12:00:00Z"),
    );
    expect(out).toMatch(/2024/);
  });
});

