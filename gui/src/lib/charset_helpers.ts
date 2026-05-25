/**
 * Pure helpers for charset sniffing and decoding of raw RFC822 bytes.
 *
 * The "view raw source" mode is debug/diagnostic — UTF-8 default is the
 * common case but real archives include Latin-1 / Windows-1252 / shift_jis
 * bodies. `resolveCharset` lets the caller honour an explicit user choice
 * or auto-sniff from the message's own `Content-Type: charset=` header.
 *
 * No DOM, no Tauri invokes, no Svelte state — unit-testable in isolation.
 */

export const AUTO_CHARSET = "auto";
export const DEFAULT_CHARSET = "utf-8";

const HEADER_SCAN_CAP_BYTES = 65_536;
const CR = 0x0d;
const LF = 0x0a;

export interface CharsetOption {
  readonly label: string;
  readonly value: string;
}

export const SUPPORTED_CHARSETS: readonly CharsetOption[] = [
  { label: "Auto", value: AUTO_CHARSET },
  { label: "UTF-8", value: "utf-8" },
  { label: "Latin-1", value: "iso-8859-1" },
  { label: "Windows-1252", value: "windows-1252" },
  { label: "Shift_JIS", value: "shift_jis" },
];

// Map non-canonical labels seen in the wild to their Encoding Standard
// canonical forms. Without this, real messages with `charset=latin-1` or
// `charset=utf8` would silently fall back to UTF-8 inside decodeWithLabel
// (TextDecoder doesn't recognise those aliases), and the user would see
// mojibake under AUTO mode plus a "(detected: latin-1)" hint that lies.
const CHARSET_ALIASES: Readonly<Record<string, string>> = {
  "latin-1": "iso-8859-1",
  latin1: "iso-8859-1",
  utf8: "utf-8",
  "utf-8-sig": "utf-8",
  cp1252: "windows-1252",
  "win-1252": "windows-1252",
  "shift-jis": "shift_jis",
  sjis: "shift_jis",
  ascii: "us-ascii",
};

export function canonicalizeCharset(label: string): string {
  const key = label.trim().toLowerCase();
  return CHARSET_ALIASES[key] ?? key;
}

function isDecodableLabel(label: string): boolean {
  try {
    new TextDecoder(label);
    return true;
  } catch {
    return false;
  }
}

function findHeaderBlockEnd(bytes: Uint8Array): number {
  // LF-LF and CRLF-CRLF are mutually exclusive within a single message: in a
  // CRLF stream every LF is preceded by CR, so the LF-LF check below cannot
  // false-match inside CRLF data. Order doesn't matter for correctness.
  const cap = Math.min(bytes.length, HEADER_SCAN_CAP_BYTES);
  for (let i = 0; i < cap - 1; i++) {
    if (bytes[i] === LF && bytes[i + 1] === LF) return i;
    if (
      i < cap - 3 &&
      bytes[i] === CR &&
      bytes[i + 1] === LF &&
      bytes[i + 2] === CR &&
      bytes[i + 3] === LF
    ) {
      return i;
    }
  }
  return -1;
}

function unfoldHeaders(headerText: string): string[] {
  const rawLines = headerText.split(/\r\n|\n/);
  const out: string[] = [];
  for (const line of rawLines) {
    if (line === "") continue;
    if ((line.startsWith(" ") || line.startsWith("\t")) && out.length > 0) {
      out[out.length - 1] = `${out[out.length - 1]} ${line.trim()}`;
    } else {
      out.push(line);
    }
  }
  return out;
}

const CONTENT_TYPE_LINE_RE = /^content-type\s*:/i;
const CHARSET_PARAM_RE = /;\s*charset\s*=\s*("[^"]*"|'[^']*'|[^\s;]+)/i;

/**
 * Extract `charset=` from the first `Content-Type:` header in RFC822 bytes.
 * Returns the lower-cased, unquoted charset label, or null if no charset is
 * declared (or if only inner MIME parts declare one — those live past the
 * top-level header/body separator and are intentionally ignored).
 *
 * Header bytes are decoded as Latin-1 (a 1:1 byte→codepoint map that never
 * throws). Only ASCII punctuation drives the regex below, so non-ASCII
 * bytes inside the header block are harmless.
 */
export function parseCharsetFromHeaders(bytes: Uint8Array): string | null {
  const endOffset = findHeaderBlockEnd(bytes);
  if (endOffset < 0) return null;
  const headerBytes = bytes.subarray(0, endOffset);
  const headerText = new TextDecoder("iso-8859-1").decode(headerBytes);
  const unfolded = unfoldHeaders(headerText);
  for (const line of unfolded) {
    if (!CONTENT_TYPE_LINE_RE.test(line)) continue;
    const match = line.match(CHARSET_PARAM_RE);
    if (!match) return null;
    let value = match[1];
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    value = value.trim().toLowerCase();
    return value === "" ? null : value;
  }
  return null;
}

/**
 * Decode `bytes` as `label`. Falls back to UTF-8 (with U+FFFD replacement)
 * when the WebView's TextDecoder rejects the label. Callers driving the
 * dropdown UX should pass a label that has already been canonicalised +
 * validated via `resolveCharset` so this fallback never silently rewrites
 * what the user thinks is being decoded.
 */
export function decodeWithLabel(bytes: Uint8Array, label: string): string {
  try {
    return new TextDecoder(label, { fatal: false }).decode(bytes);
  } catch {
    return new TextDecoder(DEFAULT_CHARSET, { fatal: false }).decode(bytes);
  }
}

/**
 * Resolve the encoding label to actually feed `decodeWithLabel`. The returned
 * label is guaranteed-decodable: it has been alias-canonicalised and probed
 * via `new TextDecoder(...)`; if either step fails the result is
 * `DEFAULT_CHARSET`. Callers may show this verbatim as "the encoding actually
 * used" without further validation.
 *
 * `sniffed` is the pre-computed return of `parseCharsetFromHeaders`. Taking it
 * as a parameter (rather than re-scanning) lets the caller cache one sniff
 * across multiple reactive derivations.
 *
 * - Explicit user choice (non-AUTO): canonicalise + validate, fall back to
 *   `DEFAULT_CHARSET` if the WebView rejects the label.
 * - AUTO with a sniffed header charset: same as above.
 * - AUTO with no declared charset: `DEFAULT_CHARSET`.
 */
export function resolveCharset(sniffed: string | null, selected: string): string {
  const candidate = selected === AUTO_CHARSET ? sniffed : selected;
  if (candidate === null) return DEFAULT_CHARSET;
  const canonical = canonicalizeCharset(candidate);
  return isDecodableLabel(canonical) ? canonical : DEFAULT_CHARSET;
}
