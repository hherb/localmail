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

  it("does not absorb later DSL tokens when a quote is left unterminated", () => {
    // Prior tokenizer behavior: unterminated quote greedily consumed the rest
    // of the input as one token, silently dropping `subject:work` and `q`.
    // Hardened behavior: re-tokenize the unterminated run by whitespace so
    // later tokens remain extractable.
    const { freeText, filters } = extractDslFilters('from:"anna subject:work q');
    expect(filters.from).toBe("anna");
    expect(filters.subject).toBe("work");
    expect(freeText).toBe("q");
  });

  it("treats apostrophes inside DSL values as literal text (not single-quote opens)", () => {
    // The prior tokenizer treated `'` as a quote char, so `from:o'brien` swallowed
    // every later token until end-of-input. Apostrophes are now literal.
    const { freeText, filters } = extractDslFilters("from:o'brien subject:report");
    expect(filters.from).toBe("o'brien");
    expect(filters.subject).toBe("report");
    expect(freeText).toBe("");
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

describe("dateFrom / dateTo / language round-trip", () => {
  it("emits after:YYYY-MM-DD from dateFrom when after is unset", () => {
    const f = emptyFilters();
    f.dateFrom = "2024-01-15";
    expect(formatDslTokens(f)).toContain("after:2024-01-15");
  });

  it("emits before:YYYY-MM-DD from dateTo when before is unset", () => {
    const f = emptyFilters();
    f.dateTo = "2024-12-31";
    expect(formatDslTokens(f)).toContain("before:2024-12-31");
  });

  it("emits lang:en from language", () => {
    const f = emptyFilters();
    f.language = "en";
    expect(formatDslTokens(f)).toContain("lang:en");
  });

  it("extractDslFilters populates dateFrom/dateTo/language from after/before/lang tokens", () => {
    const { filters } = extractDslFilters(
      "from:alice after:2024-01-15 before:2024-12-31 lang:en",
    );
    expect(filters.dateFrom).toBe("2024-01-15");
    expect(filters.dateTo).toBe("2024-12-31");
    expect(filters.language).toBe("en");
    // Legacy fields still populated for backward compat with existing wire mapping.
    expect(filters.after).toBe("2024-01-15");
    expect(filters.before).toBe("2024-12-31");
  });

  it("lang: value is lowercased on extraction", () => {
    const { filters, freeText } = extractDslFilters("lang:EN hello");
    expect(filters.language).toBe("en");
    expect(freeText).toBe("hello");
  });

  it("full round-trip: extract -> format produces equivalent DSL for new fields", () => {
    const input = "lang:de after:2024-03-01 before:2024-04-01";
    const { filters } = extractDslFilters(input);
    const formatted = formatDslTokens(filters);
    expect(formatted).toContain("after:2024-03-01");
    expect(formatted).toContain("before:2024-04-01");
    expect(formatted).toContain("lang:de");
  });
});
