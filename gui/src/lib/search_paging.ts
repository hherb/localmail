/**
 * The two pure rules the search store pages by.
 *
 * Since #308 the server lets the *cursor* decide the ordering, and rejects a
 * stated `sort` it cannot serve with a 400 rather than silently restarting the
 * search at page 1. Both rules here exist to keep the client on the right side
 * of that contract:
 *
 *   - `statedSort` — a request carrying a cursor states no sort, which is what
 *     `docs/mcp-usage.md` tells every other client ("Leave `sort` unset. The
 *     cursor already carries the ordering it continues."). Since #324 it also
 *     drops a `rank` the server cannot serve, for the same by-construction
 *     reason: both 400s become unreachable rather than merely recoverable.
 *   - `isCursorRejected` — the residual 400, which a paging request can still
 *     earn by re-sending a query that no longer feeds the cursor's walk. It is
 *     permanent for that cursor: retrying the identical request cannot clear
 *     it, so the caller must stop paging instead of looping behind a banner.
 *
 * They live together because minting a paging request and interpreting its
 * refusal are the same rule read from two ends — the call
 * `api/search_cursor.py` makes on the server side for the same reason.
 */
import { httpStatusOf } from "./admin_error";
import type { SortMode } from "./stores/search.svelte";

// The server's answer to a cursor it cannot serve (/problems/validation-failed).
const BAD_REQUEST = 400;

/**
 * The `sort` a search request should state: the caller's own when starting a
 * fresh search, nothing at all when continuing a cursor — or when the query
 * has nothing for the server to rank.
 *
 * Omitting under a cursor is not merely tidier. The store's sort is
 * user-mutable while a cursor is live, so a request that states one can pair
 * a *new* sort with an *old* cursor — the contradiction the server answers
 * with a 400.
 *
 * Omitting `rank` on a textless query is #324. The server refuses a stated
 * `sort="rank"` it cannot honour, and a blank search box with a filter chip
 * set is an ordinary shipped flow ("everything from this account, with
 * attachments") that would otherwise become an error banner. This is not a
 * fallback that loses the user's choice: an unstated sort resolves
 * server-side to the branch that will actually serve the request, which for
 * a textless query is the date walk — precisely what this flow already
 * received. `date` is still stated, because the server will serve it and
 * dropping it would make the sort selector inert for the blank-box case.
 *
 * `query` is deliberately the raw box content, not a reproduction of the
 * server's `parse_query`: the server lifts filter operators out first, so
 * `subject:invoice` reads as textless there and as text here. Matching it
 * would mean keeping a second parser in step with the first, for the cost of
 * one loud, actionable 400 on a query the user typed operators into. The
 * store's own `hasNoScope()` uses the same notion.
 */
export function statedSort(
  cursor: string | null,
  sort: SortMode,
  query: string,
): SortMode | undefined {
  if (cursor !== null) return undefined;
  if (sort === "rank" && query.trim() === "") return undefined;
  return sort;
}

/**
 * True when the server rejected the cursor as unusable for this request.
 *
 * Keyed on the status alone, deliberately: every 400 reachable from a paging
 * request says the same operational thing — this cursor and this request do
 * not go together — and no re-issue of the identical pair can succeed.
 * Contrast the 409 (`isSearchCursorExpired`), which the store recovers from
 * transparently because the request was well-formed and only the pool is gone.
 */
export function isCursorRejected(err: unknown): boolean {
  return httpStatusOf(err) === BAD_REQUEST;
}
