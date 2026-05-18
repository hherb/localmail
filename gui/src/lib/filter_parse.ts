/**
 * DSL ↔ structured-filter round-tripping.
 *
 * The UI maintains two parallel inputs for the same logical query:
 *   - SearchBar text (free-form, supports DSL)
 *   - FilterPopover form (structured)
 *
 * extractDslFilters() pulls supported DSL tokens out of the typed query and
 * surfaces them as filter chips. formatDslTokens() does the reverse — turning
 * popover state into the canonical DSL string we send to the server.
 *
 * account_id: / folder_id: tokens are intentionally NOT extracted: the tree
 * controls those, not the search box. If the user types them anyway they fall
 * through as free text and the server's parser will still apply them.
 */
import { emptyFilters, type SearchFiltersUI } from "./api/search";

const POPOVER_OPERATORS = new Set([
  "from", "to", "subject", "after", "before", "has", "lang",
]);

function tokenize(s: string): string[] {
  // Whitespace-split, respecting double-quoted runs. Apostrophes are treated
  // as literal text (so `from:o'brien` round-trips); single-quoted multi-word
  // values are not supported because the cost of breaking `o'brien` is worse
  // than the value of `'foo bar'` (use `"foo bar"` instead).
  //
  // If a quote is never closed, the unterminated run is re-processed as
  // whitespace-split tokens. The prior greedy-absorb behavior would swallow
  // every later DSL token into the unterminated run, silently dropping them.
  const out: string[] = [];
  let buf = "";
  let inQuote = false;
  let bufBeforeQuote = "";
  for (const ch of s) {
    if (inQuote) {
      if (ch === '"') {
        inQuote = false;
      } else {
        buf += ch;
      }
    } else if (ch === '"') {
      inQuote = true;
      bufBeforeQuote = buf;
    } else if (/\s/.test(ch)) {
      if (buf) { out.push(buf); buf = ""; }
    } else {
      buf += ch;
    }
  }
  if (inQuote) {
    const tail = buf.slice(bufBeforeQuote.length);
    buf = bufBeforeQuote;
    for (const ch of tail) {
      if (/\s/.test(ch)) {
        if (buf) { out.push(buf); buf = ""; }
      } else {
        buf += ch;
      }
    }
  }
  if (buf) out.push(buf);
  return out;
}

export interface ExtractedFilters {
  freeText: string;
  filters: SearchFiltersUI;
}

export function extractDslFilters(query: string): ExtractedFilters {
  const filters = emptyFilters();
  const freeParts: string[] = [];

  for (const tok of tokenize(query)) {
    const colon = tok.indexOf(":");
    if (colon > 0) {
      const op = tok.slice(0, colon).toLowerCase();
      const val = tok.slice(colon + 1);
      if (POPOVER_OPERATORS.has(op) && val) {
        if (op === "from") { filters.from = val; continue; }
        if (op === "to") { filters.to = val; continue; }
        if (op === "subject") { filters.subject = val; continue; }
        if (op === "after") {
          filters.after = val;
          filters.dateFrom = val;
          continue;
        }
        if (op === "before") {
          filters.before = val;
          filters.dateTo = val;
          continue;
        }
        if (op === "lang") {
          filters.language = val.toLowerCase();
          continue;
        }
        if (op === "has" && val.toLowerCase() === "attachment") {
          filters.hasAttachment = true;
          continue;
        }
      }
    }
    freeParts.push(tok);
  }

  return { freeText: freeParts.join(" "), filters };
}

export function formatDslTokens(f: SearchFiltersUI): string {
  const parts: string[] = [];
  if (f.from) parts.push(`from:"${f.from}"`);
  if (f.to) parts.push(`to:"${f.to}"`);
  if (f.subject) parts.push(`subject:"${f.subject}"`);
  // Prefer explicit `after`/`before` if set; otherwise fall back to dateFrom/dateTo
  // (the popover uses dateFrom/dateTo; the legacy DSL field is `after`/`before`).
  const afterVal = f.after || f.dateFrom || "";
  const beforeVal = f.before || f.dateTo || "";
  if (afterVal) parts.push(`after:${afterVal}`);
  if (beforeVal) parts.push(`before:${beforeVal}`);
  if (f.language) parts.push(`lang:${f.language}`);
  if (f.hasAttachment === true) parts.push("has:attachment");
  return parts.join(" ");
}
