/**
 * Minimal allowlist for server snippet_html: only bare <mark>/</mark> tags
 * survive. Everything else is HTML-escaped. Defense in depth against a
 * sanitizer bypass on the server side.
 */

const MARK_OPEN = /<mark>/g;
const MARK_CLOSE = /<\/mark>/g;
const MARK_OPEN_PLACEHOLDER = "LOCALMAIL_MARK_OPEN";
const MARK_CLOSE_PLACEHOLDER = "LOCALMAIL_MARK_CLOSE";

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function sanitizeSnippet(snippet: string | null): string {
  if (!snippet) return "";
  // 1. Replace exact-match <mark>/</mark> with placeholders so escaping
  //    doesn't touch them. Anything else (attrs, weird casing, other tags)
  //    falls through and gets escaped.
  // Count opens first so we only restore as many closes as there are paired opens.
  const openCount = (snippet.match(MARK_OPEN) ?? []).length;
  let closesReplaced = 0;
  const guarded = snippet
    .replace(MARK_OPEN, MARK_OPEN_PLACEHOLDER)
    .replace(MARK_CLOSE, (_) => {
      if (closesReplaced < openCount) { closesReplaced++; return MARK_CLOSE_PLACEHOLDER; }
      return "</mark>";
    });
  // 2. Escape everything.
  const escaped = escapeHtml(guarded);
  // 3. Restore the placeholders as real tags.
  return escaped
    .split(MARK_OPEN_PLACEHOLDER).join("<mark>")
    .split(MARK_CLOSE_PLACEHOLDER).join("</mark>");
}
