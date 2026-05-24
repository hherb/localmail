import { describe, it, expect } from "vitest";
import { dedupNewMessages, parseCursor, POLL_INTERVAL_MS } from "./change_helpers";
import type { MessageSummary } from "./api/changes";

function ms(id: string): MessageSummary {
  return {
    message_id: id,
    subject: null,
    from: { address: null, name: null },
    to: [],
    date: null,
    account: { id: "1", name: null },
    folder: null,
    snippet_html: null,
    has_attachments: false,
  } as unknown as MessageSummary;
}

describe("dedupNewMessages", () => {
  it("returns input untouched when nothing overlaps", () => {
    const existing = [ms("1"), ms("2")];
    const incoming = [ms("3"), ms("4")];
    expect(dedupNewMessages(existing, incoming)).toEqual(incoming);
  });

  it("filters out messages already present", () => {
    const existing = [ms("1"), ms("2")];
    const incoming = [ms("2"), ms("3")];
    expect(dedupNewMessages(existing, incoming)).toEqual([ms("3")]);
  });

  it("returns [] when all incoming are duplicates", () => {
    const existing = [ms("1"), ms("2")];
    const incoming = [ms("1"), ms("2")];
    expect(dedupNewMessages(existing, incoming)).toEqual([]);
  });
});

describe("parseCursor", () => {
  it("treats null as no cursor", () => expect(parseCursor(null)).toBeNull());
  it("treats empty string as no cursor", () => expect(parseCursor("")).toBeNull());
  it("preserves a numeric string", () => expect(parseCursor("12345")).toBe("12345"));
});

describe("constants", () => {
  it("polls every 30s by default", () => expect(POLL_INTERVAL_MS).toBe(30000));
});
