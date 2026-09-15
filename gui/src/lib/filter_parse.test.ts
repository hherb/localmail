import { describe, expect, it } from "vitest";
import {
  absorbConflictingOperators,
  extractDslFilters,
  formatDslTokens,
  joinTokens,
  tokenize,
} from "./filter_parse";
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

describe("absorbConflictingOperators", () => {
  // The server composes structured filters ahead of the query text and keeps
  // the last value of a scalar operator (#367), so a typed operator beats the
  // chip. These pin that the chip is made to say so.

  it("moves a typed from: that contradicts the chip into the chip", () => {
    const chips = { ...emptyFilters(), from: "alice" };
    const out = absorbConflictingOperators("from:bob invoice", chips);
    expect(out.filters.from).toBe("bob");
    expect(out.query).toBe("invoice");
  });

  it("moves to:, subject:, after: and before: the same way", () => {
    const chips = {
      ...emptyFilters(), to: "carol", subject: "old",
      after: "2024-01-01", dateFrom: "2024-01-01",
      before: "2024-12-31", dateTo: "2024-12-31",
    };
    const out = absorbConflictingOperators(
      "to:dave subject:new after:2019-01-01 before:2019-06-01 x", chips);
    expect(out.filters).toMatchObject({
      to: "dave", subject: "new",
      after: "2019-01-01", dateFrom: "2019-01-01",
      before: "2019-06-01", dateTo: "2019-06-01",
    });
    expect(out.query).toBe("x");
  });

  it("reads a date chip set only through dateFrom/dateTo", () => {
    const chips = { ...emptyFilters(), dateFrom: "2024-01-01" };
    const out = absorbConflictingOperators("after:2019-01-01 x", chips);
    expect(out.filters.after).toBe("2019-01-01");
    expect(out.filters.dateFrom).toBe("2019-01-01");
    expect(out.query).toBe("x");
  });

  it("leaves the query alone when no chip is contradicted", () => {
    const chips = { ...emptyFilters(), subject: "q3" };
    const query = 'from:bob "exact phrase" it\'s';
    const out = absorbConflictingOperators(query, chips);
    // The same objects back, so the store can tell nothing changed.
    expect(out.query).toBe(query);
    expect(out.filters).toBe(chips);
  });

  it("leaves a typed operator that matches its chip where it is", () => {
    const chips = { ...emptyFilters(), from: "bob" };
    const out = absorbConflictingOperators("from:bob invoice", chips);
    expect(out.query).toBe("from:bob invoice");
    expect(out.filters).toBe(chips);
  });

  it("keeps every other token, quoting one with whitespace or an apostrophe", () => {
    const chips = { ...emptyFilters(), from: "alice" };
    const out = absorbConflictingOperators(
      'to:"bob smith" from:bob "exact phrase" don\'t', chips);
    expect(out.query).toBe('"to:bob smith" "exact phrase" "don\'t"');
    // Re-quoted, the kept operator still reads as one filter.
    expect(extractDslFilters(out.query).filters.to).toBe("bob smith");
  });

  it.each([
    // The server's tokenizer opens a quote at a bare apostrophe and swallows
    // every later token, so these filters were lost when the one-word token
    // was re-joined unquoted (review of #376).
    ['"don\'t" has:attachment from:bob', '"don\'t" has:attachment'],
    ['from:bob "O\'Brien" lang:de', '"O\'Brien" lang:de'],
    ['from:bob "it\'s" to:carol', '"it\'s" to:carol'],
    ['subject:"it\'s" from:bob has:attachment', '"subject:it\'s" has:attachment'],
  ])("quotes a kept apostrophe token so no later filter is swallowed: %s", (query, expected) => {
    const out = absorbConflictingOperators(query, { ...emptyFilters(), from: "alice" });
    expect(out.query).toBe(expected);
  });

  it("removes every typed token for an absorbed operator, keeping the last value", () => {
    const chips = { ...emptyFilters(), from: "alice" };
    const out = absorbConflictingOperators("from:bob x from:carol", chips);
    expect(out.filters.from).toBe("carol");
    expect(out.query).toBe("x");
  });

  it("absorbs an operator the server's tokenizer would have hidden", () => {
    // Server-side, the apostrophe in `don't` opens a quote that swallows
    // `from:bob` into free text, so the server applied the chip's alice and
    // searched for the words "dont from:bob". The typed value is what the
    // user asked for, so it is applied — not what the server would have done.
    const chips = { ...emptyFilters(), from: "alice" };
    const out = absorbConflictingOperators("don't from:bob", chips);
    expect(out.filters.from).toBe("bob");
    expect(out.query).toBe('"don\'t"');
  });

  it("reads a before chip set only through dateTo", () => {
    const chips = { ...emptyFilters(), dateTo: "2024-12-31" };
    const out = absorbConflictingOperators("before:2019-06-01 x", chips);
    expect(out.filters.before).toBe("2019-06-01");
    expect(out.filters.dateTo).toBe("2019-06-01");
    expect(out.query).toBe("x");
  });

  it("leaves a typed date that matches its chip where it is", () => {
    const chips = { ...emptyFilters(), after: "2024-01-01", dateFrom: "2024-01-01" };
    const out = absorbConflictingOperators("after:2024-01-01 x", chips);
    expect(out.query).toBe("after:2024-01-01 x");
    expect(out.filters).toBe(chips);
  });

  it("leaves a typed date that is not a date in the query", () => {
    // The server refuses it by name from the query. Moved into the chip it
    // would be refused as the structured filter, and the chip would carry it.
    const chips = { ...emptyFilters(), after: "2024-01-01", dateFrom: "2024-01-01" };
    const out = absorbConflictingOperators("after:last-week x", chips);
    expect(out.query).toBe("after:last-week x");
    expect(out.filters).toBe(chips);
  });

  it("does not touch has: or lang:", () => {
    // The server refuses a has: conflict outright and unions lang, so neither
    // silently out-votes its chip.
    const chips = { ...emptyFilters(), hasAttachment: true, language: "en" };
    const out = absorbConflictingOperators("has:attachment lang:de x", chips);
    expect(out.query).toBe("has:attachment lang:de x");
    expect(out.filters).toBe(chips);
  });
});

describe("joinTokens", () => {
  // The query the client sends after absorbing must tokenize to the same
  // tokens on the server, whose tokenizer also opens a quote at `'`. Inside
  // `"` both treat `'` as literal, and the tokenizer never leaves a `"` in a
  // token, so a quoted token cannot unbalance anything after it.
  it.each([
    [["hello", "world"], "hello world"],
    [["don't"], '"don\'t"'],
    [["to:bob smith", "x"], '"to:bob smith" x'],
    [["a\tb", "it's", "q"], '"a\tb" "it\'s" q'],
  ])("joins %j as %s", (tokens, expected) => {
    expect(joinTokens(tokens)).toBe(expected);
  });

  it.each([
    'from:bob "O\'Brien" lang:de',
    '"unclosed phrase from:x',
    "it's from:o'brien has:attachment",
    'ab"c d"e \'x y\' "z\'s"',
    'to:"a\tb" \'',
  ])("gives back the tokens it was given: %s", (query) => {
    const tokens = tokenize(query);
    expect(tokenize(joinTokens(tokens))).toEqual(tokens);
  });

  it("a bare join loses a multi-word token (the negative control)", () => {
    // The negative control for the test above: a bare join of these tokens
    // does not round-trip, which is the defect the quoting exists for.
    const tokens = tokenize('"to:bob smith" x');
    expect(tokenize(tokens.join(" "))).not.toEqual(tokens);
  });
});
