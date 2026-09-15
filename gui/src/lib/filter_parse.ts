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

export function tokenize(s: string): string[] {
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

// The popover operator a token names, with its value, or null. One rule for
// extracting a token and for removing it, so the two cannot disagree.
function popoverOperator(tok: string): { op: string; val: string } | null {
  const colon = tok.indexOf(":");
  if (colon <= 0) return null;
  const op = tok.slice(0, colon).toLowerCase();
  const val = tok.slice(colon + 1);
  return POPOVER_OPERATORS.has(op) && val ? { op, val } : null;
}

export function extractDslFilters(query: string): ExtractedFilters {
  const filters = emptyFilters();
  const freeParts: string[] = [];

  for (const tok of tokenize(query)) {
    const operator = popoverOperator(tok);
    if (operator) {
      const { op, val } = operator;
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
    freeParts.push(tok);
  }

  return { freeText: joinTokens(freeParts), filters };
}

/**
 * Join tokens into a query that tokenizes back to exactly those tokens — on
 * the server as well as here.
 *
 * A bare `join(" ")` does neither. The tokenizer above drops quote
 * characters, so a multi-word token (`to:bob smith`) splits in two. And the
 * server's tokenizer also opens a quote at `'`, so a bare `don't` swallows
 * every token after it, filters included. A token holding whitespace or an
 * apostrophe is therefore wrapped in `"`: inside it both tokenizers read `'`
 * as a literal, and no token contains a `"` to unbalance the wrapping.
 */
export function joinTokens(tokens: string[]): string {
  return tokens.map((tok) => (/[\s']/.test(tok) ? `"${tok}"` : tok)).join(" ");
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

// The server's own shape for a structured date (`api.search._DATE_SHAPE`).
const DATE_SHAPE = /^\d{4}-\d{1,2}-\d{1,2}$/;

export interface AbsorbedQuery {
  query: string;
  filters: SearchFiltersUI;
}

/**
 * Settle a typed operator that contradicts a filter chip in the typed
 * operator's favour, and make the chip say so.
 *
 * The server composes structured filters ahead of the query text and keeps
 * the last value of a scalar operator (#367), so `from:bob` typed over a
 * "From: alice" chip searches for bob while the chip still reads alice. On
 * such a conflict the typed value replaces the chip's and its tokens leave
 * the query, which is rebuilt with `joinTokens`. The value then reaches the
 * server as a structured filter, and the rest of the query as exactly the
 * tokens this module read, so the chips are what is applied.
 *
 * Only a conflict is touched: a typed operator with no chip, or one matching
 * its chip, stays in the box, and the same objects come back. `has:` and
 * `lang:` are left alone — the server refuses a `has:` conflict outright and
 * unions `lang`, so neither silently out-votes its chip. A typed date is
 * absorbed only when it is shaped like one: `after:last-week` stays in the
 * query, where the server refuses it by name, rather than riding in a chip.
 *
 * Two imprecisions, both because this reads the query with the tokenizer
 * above rather than the server's, which also opens a quote at `'`:
 * - an operator only the server extracts (`'from:bob'`, single-quoted) is
 *   not seen, so its chip can still be out-voted;
 * - an operator an earlier apostrophe hid from the server (`don't from:bob`)
 *   *is* seen and absorbed, so the applied value becomes the typed one where
 *   the server would have kept the chip's. That is what was typed.
 */
export function absorbConflictingOperators(
  query: string,
  filters: SearchFiltersUI,
): AbsorbedQuery {
  const typed = extractDslFilters(query).filters;
  const next: SearchFiltersUI = { ...filters };
  const absorbed = new Set<string>();

  for (const key of ["from", "to", "subject"] as const) {
    if (typed[key] && filters[key] && typed[key] !== filters[key]) {
      next[key] = typed[key];
      absorbed.add(key);
    }
  }
  const chipAfter = filters.after || filters.dateFrom || "";
  if (DATE_SHAPE.test(typed.after) && chipAfter && typed.after !== chipAfter) {
    next.after = typed.after;
    next.dateFrom = typed.after;
    absorbed.add("after");
  }
  const chipBefore = filters.before || filters.dateTo || "";
  if (DATE_SHAPE.test(typed.before) && chipBefore && typed.before !== chipBefore) {
    next.before = typed.before;
    next.dateTo = typed.before;
    absorbed.add("before");
  }

  if (absorbed.size === 0) return { query, filters };
  const kept = tokenize(query).filter((tok) => {
    const operator = popoverOperator(tok);
    return !(operator && absorbed.has(operator.op));
  });
  return { query: joinTokens(kept), filters: next };
}
