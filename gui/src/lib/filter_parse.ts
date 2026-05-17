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
  "from", "to", "subject", "after", "before", "has",
]);

function tokenize(s: string): string[] {
  // Whitespace-split, but respect quoted runs (single or double quotes).
  const out: string[] = [];
  let buf = "";
  let quote: string | null = null;
  for (const ch of s) {
    if (quote !== null) {
      if (ch === quote) {
        quote = null;
      } else {
        buf += ch;
      }
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (/\s/.test(ch)) {
      if (buf) { out.push(buf); buf = ""; }
    } else {
      buf += ch;
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
        if (op === "after") { filters.after = val; continue; }
        if (op === "before") { filters.before = val; continue; }
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
  if (f.after) parts.push(`after:${f.after}`);
  if (f.before) parts.push(`before:${f.before}`);
  if (f.hasAttachment === true) parts.push("has:attachment");
  return parts.join(" ");
}
