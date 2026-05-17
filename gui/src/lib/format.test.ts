import { describe, expect, it } from "vitest";
import {
  addressLabel,
  formatRelativeDate,
  selectionMatches,
  truncate,
} from "./format";
import type { MessageAddress, MessageSummary, Selection } from "./tauri";

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
    const out = formatRelativeDate(
      "2026-05-17T09:30:00Z",
      new Date("2026-05-17T15:00:00Z"),
    );
    // Time format is locale-dependent; just assert it contains a digit and a colon.
    expect(out).toMatch(/\d+:\d+/);
  });

  it("returns short date for earlier in the same year", () => {
    const out = formatRelativeDate(
      "2026-03-03T08:14:00Z",
      new Date("2026-05-17T12:00:00Z"),
    );
    // Format like "Mar 3"; assert month abbreviation present.
    expect(out.toLowerCase()).toMatch(/[a-z]{3}\s+\d+/);
  });

  it("returns full date for older messages", () => {
    const out = formatRelativeDate(
      "2024-12-25T08:14:00Z",
      new Date("2026-05-17T12:00:00Z"),
    );
    expect(out).toMatch(/2024/);
  });
});

describe("selectionMatches", () => {
  const mkMsg = (accountId: string): MessageSummary => ({
    message_id: "1",
    subject: "x",
    from: { name: null, address: null },
    date: null,
    account: { id: accountId, name: null },
  });

  it('"all" matches every message', () => {
    const sel: Selection = { kind: "all" };
    expect(selectionMatches(sel, mkMsg("1"))).toBe(true);
    expect(selectionMatches(sel, mkMsg("2"))).toBe(true);
  });

  it('"account" matches messages of that account', () => {
    const sel: Selection = { kind: "account", accountId: "1" };
    expect(selectionMatches(sel, mkMsg("1"))).toBe(true);
    expect(selectionMatches(sel, mkMsg("2"))).toBe(false);
  });

  it('"folder" filters by account (folder narrowing is server-side, deferred)', () => {
    const sel: Selection = { kind: "folder", accountId: "1", folderId: "5" };
    expect(selectionMatches(sel, mkMsg("1"))).toBe(true);
    expect(selectionMatches(sel, mkMsg("2"))).toBe(false);
  });
});
