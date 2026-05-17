import { describe, expect, it } from "vitest";
import { extractDslFilters, formatDslTokens } from "./filter_parse";
import { emptyFilters } from "./api/search";

describe("extractDslFilters", () => {
  it("returns empty filters and unchanged free text for a plain query", () => {
    const { freeText, filters } = extractDslFilters("hello world");
    expect(freeText).toBe("hello world");
    expect(filters).toEqual(emptyFilters());
  });

  it("extracts from: token", () => {
    const { freeText, filters } = extractDslFilters("from:anna receipts");
    expect(freeText).toBe("receipts");
    expect(filters.from).toBe("anna");
  });

  it("extracts has:attachment token", () => {
    const { freeText, filters } = extractDslFilters("has:attachment school");
    expect(filters.hasAttachment).toBe(true);
    expect(freeText).toBe("school");
  });

  it("extracts after: and before:", () => {
    const { filters } = extractDslFilters("after:2024-01-01 before:2024-12-31 q");
    expect(filters.after).toBe("2024-01-01");
    expect(filters.before).toBe("2024-12-31");
  });

  it("does not extract account_id: (those come from the tree, not user typing)", () => {
    const { freeText, filters } = extractDslFilters("account_id:5 stuff");
    // Falls through as free text — UI doesn't surface account_id as a chip.
    expect(filters.accountIds).toEqual([]);
    expect(freeText).toContain("account_id:5");
  });

  it("preserves quoted values", () => {
    const { filters } = extractDslFilters('from:"anna h" subject:"the trip"');
    expect(filters.from).toBe("anna h");
    expect(filters.subject).toBe("the trip");
  });
});

describe("formatDslTokens", () => {
  it("returns empty string when no popover filters set", () => {
    expect(formatDslTokens(emptyFilters())).toBe("");
  });

  it("emits from:VALUE for a populated from", () => {
    const f = emptyFilters();
    f.from = "anna";
    expect(formatDslTokens(f)).toBe('from:"anna"');
  });

  it("emits has:attachment when hasAttachment===true", () => {
    const f = emptyFilters();
    f.hasAttachment = true;
    expect(formatDslTokens(f)).toBe("has:attachment");
  });

  it("emits multiple tokens space-separated in stable order", () => {
    const f = emptyFilters();
    f.from = "anna"; f.subject = "trip"; f.hasAttachment = true;
    expect(formatDslTokens(f)).toBe('from:"anna" subject:"trip" has:attachment');
  });

  it("skips empty strings", () => {
    const f = emptyFilters();
    f.from = "anna"; f.to = ""; f.subject = "";
    expect(formatDslTokens(f)).toBe('from:"anna"');
  });

  it("does NOT emit account_ids/folder_ids tokens (those go through filters wire)", () => {
    const f = emptyFilters();
    f.accountIds = ["5"]; f.from = "x";
    expect(formatDslTokens(f)).toBe('from:"x"');
  });
});
