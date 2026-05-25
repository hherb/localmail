import { describe, expect, it } from "vitest";

import {
  AUTO_CHARSET,
  DEFAULT_CHARSET,
  SUPPORTED_CHARSETS,
  canonicalizeCharset,
  decodeWithLabel,
  parseCharsetFromHeaders,
  resolveCharset,
} from "./charset_helpers";

function bytes(s: string): Uint8Array {
  return new TextEncoder().encode(s);
}

function latin1Bytes(codepoints: number[]): Uint8Array {
  return Uint8Array.from(codepoints);
}

// Render a string as `"<glyph>" (U+XXXX U+YYYY …)` so an assertion failure
// between two visually-ambiguous strings (e.g. "€" vs U+0080's control-glyph
// "" which some terminals render identically as "^@" or nothing) prints
// the actual codepoints in brackets next to the glyph form.
function annotateCodepoints(s: string): string {
  const hexParts = [...s].map((c) => `U+${c.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0")}`);
  return `"${s}" (${hexParts.join(" ")})`;
}

function expectDecodedEquals(actual: string, expected: string): void {
  if (actual === expected) return;
  throw new Error(
    `Decoded string mismatch:\n  expected: ${annotateCodepoints(expected)}\n  received: ${annotateCodepoints(actual)}`,
  );
}

describe("parseCharsetFromHeaders", () => {
  it("returns null when there is no header/body separator", () => {
    expect(parseCharsetFromHeaders(bytes("Content-Type: text/plain"))).toBeNull();
  });

  it("returns null when Content-Type has no charset parameter", () => {
    const raw = "Content-Type: text/plain\r\nFrom: a@b\r\n\r\nbody";
    expect(parseCharsetFromHeaders(bytes(raw))).toBeNull();
  });

  it("extracts charset from a plain Content-Type header", () => {
    const raw = "Content-Type: text/plain; charset=utf-8\r\n\r\nbody";
    expect(parseCharsetFromHeaders(bytes(raw))).toBe("utf-8");
  });

  it("is case-insensitive on the header name and parameter name", () => {
    const raw = "CONTENT-TYPE: text/plain; CHARSET=Windows-1252\r\n\r\nbody";
    expect(parseCharsetFromHeaders(bytes(raw))).toBe("windows-1252");
  });

  it("strips double quotes around the charset value", () => {
    const raw = 'Content-Type: text/plain; charset="ISO-8859-1"\r\n\r\nbody';
    expect(parseCharsetFromHeaders(bytes(raw))).toBe("iso-8859-1");
  });

  it("strips single quotes around the charset value", () => {
    const raw = "Content-Type: text/plain; charset='Shift_JIS'\r\n\r\nbody";
    expect(parseCharsetFromHeaders(bytes(raw))).toBe("shift_jis");
  });

  it("tolerates whitespace around the equals sign", () => {
    const raw = "Content-Type: text/plain; charset = utf-8\r\n\r\nbody";
    expect(parseCharsetFromHeaders(bytes(raw))).toBe("utf-8");
  });

  it("handles a folded Content-Type header", () => {
    const raw =
      "Content-Type: text/plain;\r\n charset=utf-8;\r\n format=flowed\r\n\r\nbody";
    expect(parseCharsetFromHeaders(bytes(raw))).toBe("utf-8");
  });

  it("handles LF-only line endings", () => {
    const raw = "Content-Type: text/plain; charset=utf-8\n\nbody";
    expect(parseCharsetFromHeaders(bytes(raw))).toBe("utf-8");
  });

  it("does not bleed into MIME inner-part headers when the top-level has no Content-Type", () => {
    const raw =
      "From: a@b\r\n" +
      "Subject: hi\r\n" +
      "\r\n" +
      "--bound\r\n" +
      "Content-Type: text/plain; charset=windows-1252\r\n" +
      "\r\n" +
      "body";
    expect(parseCharsetFromHeaders(bytes(raw))).toBeNull();
  });

  it("returns the first Content-Type when more than one appears in the header block", () => {
    const raw =
      "Content-Type: text/plain; charset=utf-8\r\n" +
      "Content-Type: text/plain; charset=latin-1\r\n" +
      "\r\nbody";
    expect(parseCharsetFromHeaders(bytes(raw))).toBe("utf-8");
  });

  it("returns null when the value is an empty quoted string", () => {
    const raw = 'Content-Type: text/plain; charset=""\r\n\r\nbody';
    expect(parseCharsetFromHeaders(bytes(raw))).toBeNull();
  });

  it("ignores non-ASCII bytes in the header block without crashing", () => {
    const header = "Content-Type: text/plain; charset=iso-8859-1\r\n\r\n";
    const body = latin1Bytes([0xe9, 0xe8, 0xe7]);
    const all = new Uint8Array(header.length + body.length);
    all.set(new TextEncoder().encode(header), 0);
    all.set(body, header.length);
    expect(parseCharsetFromHeaders(all)).toBe("iso-8859-1");
  });
});

describe("decodeWithLabel", () => {
  it("decodes valid UTF-8 bytes with the utf-8 label", () => {
    const input = new TextEncoder().encode("café");
    expectDecodedEquals(decodeWithLabel(input, "utf-8"), "café");
  });

  it("decodes latin-1 bytes with the iso-8859-1 label", () => {
    expectDecodedEquals(
      decodeWithLabel(latin1Bytes([0x63, 0x61, 0x66, 0xe9]), "iso-8859-1"),
      "café",
    );
  });

  it("decodes windows-1252 specific bytes", () => {
    expectDecodedEquals(decodeWithLabel(latin1Bytes([0x80]), "windows-1252"), "€");
  });

  it("falls back to utf-8 when the label is not a known encoding", () => {
    const input = new TextEncoder().encode("hello");
    expect(decodeWithLabel(input, "not-a-real-encoding")).toBe("hello");
  });

  it("does not throw on invalid byte sequences (uses replacement, not fatal)", () => {
    const input = latin1Bytes([0xff, 0xfe, 0xfd]);
    expect(() => decodeWithLabel(input, "utf-8")).not.toThrow();
  });
});

describe("canonicalizeCharset", () => {
  it("maps latin-1 to iso-8859-1", () => {
    expect(canonicalizeCharset("latin-1")).toBe("iso-8859-1");
  });

  it("maps latin1 (no dash) to iso-8859-1", () => {
    expect(canonicalizeCharset("latin1")).toBe("iso-8859-1");
  });

  it("maps utf8 to utf-8", () => {
    expect(canonicalizeCharset("utf8")).toBe("utf-8");
  });

  it("maps cp1252 to windows-1252", () => {
    expect(canonicalizeCharset("cp1252")).toBe("windows-1252");
  });

  it("maps shift-jis (hyphen variant) to shift_jis", () => {
    expect(canonicalizeCharset("shift-jis")).toBe("shift_jis");
  });

  it("lowercases input before alias lookup", () => {
    expect(canonicalizeCharset("LATIN-1")).toBe("iso-8859-1");
    expect(canonicalizeCharset("UTF8")).toBe("utf-8");
  });

  it("trims surrounding whitespace before alias lookup", () => {
    expect(canonicalizeCharset("  latin-1  ")).toBe("iso-8859-1");
  });

  it("returns the input unchanged when no alias matches", () => {
    expect(canonicalizeCharset("iso-8859-1")).toBe("iso-8859-1");
    expect(canonicalizeCharset("utf-8")).toBe("utf-8");
    expect(canonicalizeCharset("windows-1252")).toBe("windows-1252");
  });
});

describe("resolveCharset", () => {
  it("returns the user-selected label verbatim when it is not AUTO", () => {
    expect(resolveCharset(null, "windows-1252")).toBe("windows-1252");
  });

  it("uses the sniffed value when AUTO and a charset was sniffed", () => {
    expect(resolveCharset("iso-8859-1", AUTO_CHARSET)).toBe("iso-8859-1");
  });

  it("falls back to DEFAULT_CHARSET when AUTO and no charset was sniffed", () => {
    expect(resolveCharset(null, AUTO_CHARSET)).toBe(DEFAULT_CHARSET);
  });

  it("canonicalises non-canonical sniffed aliases (latin-1 → iso-8859-1)", () => {
    expect(resolveCharset("latin-1", AUTO_CHARSET)).toBe("iso-8859-1");
  });

  it("canonicalises non-canonical user-selected aliases too", () => {
    expect(resolveCharset(null, "utf8")).toBe("utf-8");
  });

  it("falls back to DEFAULT_CHARSET when the sniffed label is not decodable and has no alias", () => {
    expect(resolveCharset("not-a-real-encoding", AUTO_CHARSET)).toBe(DEFAULT_CHARSET);
  });

  it("ignores the sniffed value when a manual selection overrides AUTO", () => {
    expect(resolveCharset("iso-8859-1", "windows-1252")).toBe("windows-1252");
  });
});

describe("SUPPORTED_CHARSETS", () => {
  it("starts with the AUTO entry so it can be the dropdown default", () => {
    expect(SUPPORTED_CHARSETS[0]?.value).toBe(AUTO_CHARSET);
  });

  it("includes every charset that decodeWithLabel must handle", () => {
    const values = SUPPORTED_CHARSETS.map((c) => c.value);
    expect(values).toContain("utf-8");
    expect(values).toContain("iso-8859-1");
    expect(values).toContain("windows-1252");
    expect(values).toContain("shift_jis");
  });
});
