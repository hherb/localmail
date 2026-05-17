/**
 * Minimal allowlist for server snippet_html: only bare <mark>/</mark> tags
 * survive. Everything else is HTML-escaped. Defense in depth against a
 * sanitizer bypass on the server side.
 */

const MARK_OPEN = /<mark>/g;
const MARK_CLOSE = /<\/mark>/g;

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Per-call nonce so a server payload that contains the literal placeholder
// string cannot smuggle <mark> tags through the restore step.
function makeNonce(): string {
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    const buf = new Uint8Array(8);
    crypto.getRandomValues(buf);
    return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  return Math.random().toString(36).slice(2, 18).padEnd(16, "0");
}

export function sanitizeSnippet(snippet: string | null): string {
  if (!snippet) return "";
  const nonce = makeNonce();
  const openPh = `LOCALMAIL_MARK_OPEN_${nonce}`;
  const closePh = `LOCALMAIL_MARK_CLOSE_${nonce}`;
  // Count opens first so we only restore as many closes as there are paired opens.
  const openCount = (snippet.match(MARK_OPEN) ?? []).length;
  let closesReplaced = 0;
  const guarded = snippet
    .replace(MARK_OPEN, openPh)
    .replace(MARK_CLOSE, (_) => {
      if (closesReplaced < openCount) { closesReplaced++; return closePh; }
      return "</mark>";
    });
  const escaped = escapeHtml(guarded);
  return escaped.split(openPh).join("<mark>").split(closePh).join("</mark>");
}
