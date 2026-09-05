/**
 * The two pure rules the sort selector renders by.
 *
 * `sort` is a *request*. Since #324 the server resolves it from the query
 * rather than honouring it: a query with no free text — blank, or only filter
 * operators — has nothing for the hybrid pool to rank, so it is served
 * date-ordered whatever was asked for. The selector used to render the
 * request, so a textless search showed **Relevance** checked over
 * date-ordered rows, and clicking Relevance re-ran the search and changed
 * nothing — the inert-control pattern #148 names as a defect, here with the
 * label asserting an ordering that was not in effect (#345).
 *
 * The server now reports its resolution as `sort_applied`, and these rules
 * turn that into what the selector shows. They live together because
 * "which one is checked" and "which one is available" are one reading of the
 * same field — the call `search_paging.ts` makes for the paging pair.
 *
 * Neither rule inspects the query. That is deliberate and is the same
 * constraint `statedSort` documents: the server decides "textless" only
 * after lifting filter operators out, so `from:alice` and `has:attachment`
 * are textless there while reading as text to any client-side test of the
 * raw box. Reproducing `parse_query` in the client is rejected; reading the
 * answer the server already sent is not.
 */
import type { SortMode } from "./stores/search.svelte";

/**
 * Which radio is checked.
 *
 * The ordering that ran, once anything has run. Before the first search
 * there is nothing to reflect, so the user's stored preference stands — which
 * is also what makes toggling pre-search meaningful (`SearchBar` only
 * re-submits when a search is already on screen).
 *
 * `applied` deliberately keeps describing the results **on screen** rather
 * than being cleared when the query box is edited. Clearing it would flip the
 * selector back to the request while date-ordered rows are still displayed,
 * which is the exact mismatch this exists to end.
 */
export function displayedSort(
  requested: SortMode,
  applied: SortMode | null,
): SortMode {
  return applied ?? requested;
}

/**
 * True when the server has said this query cannot be ranked.
 *
 * The proof is a resolution that disagrees with the request: `statedSort`
 * never sends `rank`, so a `rank` preference reaches the server as "unstated"
 * and comes back `date` only when the query had nothing to rank. Relevance is
 * then genuinely unavailable, not merely unselected, and the radio is
 * disabled with `RELEVANCE_UNAVAILABLE_REASON` rather than left inert.
 *
 * A `date` request tells us nothing either way — rank may well have been
 * available and simply not chosen — so it is never read as unavailable.
 * **Known imprecision, deliberate:** an explicit Date selection on a textless
 * query therefore leaves Relevance enabled until it is clicked once, at which
 * point the request becomes `rank`, the answer comes back `date`, and the
 * selector both corrects itself and explains why. Judging it earlier means
 * knowing the resolution before asking for it — a second parser, which is
 * what this file exists not to have. Failing this way costs one click and
 * never claims Relevance is unavailable when it is.
 */
export function relevanceUnavailable(
  requested: SortMode,
  applied: SortMode | null,
): boolean {
  return applied === "date" && requested === "rank";
}

/** Why Relevance is disabled. Shown as the control's `title`. */
export const RELEVANCE_UNAVAILABLE_REASON =
  "Add search text to rank by relevance — filters alone are ordered by date";
