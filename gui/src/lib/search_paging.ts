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
 *     cursor already carries the ordering it continues."). Since #324 it never
 *     states `rank` at all, for the same by-construction reason: both 400s
 *     become unreachable rather than merely recoverable.
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
 * The `sort` a search request should state: `date` when starting a fresh
 * search with it selected, nothing at all otherwise.
 *
 * Omitting under a cursor is not merely tidier. The store's sort is
 * user-mutable while a cursor is live, so a request that states one can pair
 * a *new* sort with an *old* cursor — the contradiction the server answers
 * with a 400.
 *
 * **`rank` is never stated**, which is #324. Stating it is never necessary:
 * the server resolves an unstated sort to the branch that will actually serve
 * the request — `rank` as soon as there is text to rank, `date` when there is
 * not — so omitting it is *identical* to stating it wherever stating it would
 * have been honoured, and avoids the 400 wherever it would not.
 *
 * That equivalence is what lets this rule ignore the query entirely, and
 * ignoring it is the point. The server decides "textless" only *after*
 * lifting filter operators out, so `from:alice` and `has:attachment` — the
 * two shapes `SearchBar`'s own placeholder advertises — are textless there
 * while reading as text to any client-side test of the raw box. A rule that
 * inspected the query would have to reproduce `parse_query` to get them
 * right, and one that inspected it *naively* turns those advertised searches
 * into an error banner. Neither is needed once `rank` is simply never stated.
 *
 * `date` is still stated, because the server serves it for any query and
 * dropping it would make the sort selector inert.
 */
export function statedSort(
  cursor: string | null,
  sort: SortMode,
): SortMode | undefined {
  if (cursor !== null) return undefined;
  return sort === "rank" ? undefined : sort;
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
