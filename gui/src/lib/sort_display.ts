/**
 * The two pure rules the sort selector renders by.
 *
 * `sort` is a *request*. Since #324 the server resolves it from the query
 * rather than honouring it: a query with no free text — blank, or only filter
 * operators — has nothing for the hybrid pool to rank, so it is served
 * date-ordered whatever was asked for. The selector used to render the
 * request, so a textless search showed **Relevance** checked over
 * date-ordered rows — the label asserting an ordering that was not in
 * effect (#345). From an explicit Date selection, clicking Relevance
 * additionally re-ran the search and changed nothing; from the default
 * state the click fired no `change` event at all, so the control was inert
 * in one state and lying in both. Either way it is the pattern #148 names.
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
 * never claims Relevance is unavailable when it was available *for the
 * results on screen*.
 *
 * That scoping is the honest form of the guarantee. `applied` deliberately
 * outlives an edit to the query box (see `displayedSort`), so after a
 * textless search, typing rankable text leaves Relevance disabled until the
 * next submit. The flag still describes the displayed rows correctly; it is
 * only stale with respect to the box, which is why
 * `RELEVANCE_UNAVAILABLE_REASON` is worded after the rows rather than as an
 * instruction about the box.
 */
export function relevanceUnavailable(
  requested: SortMode,
  applied: SortMode | null,
): boolean {
  return applied === "date" && requested === "rank";
}

/**
 * The applied sort a response reports, or `null` when it reports none.
 *
 * The one place the wire value becomes a `SortMode`, so the store's field
 * and `displayedSort`'s return type are true at runtime rather than merely
 * declared. Nothing else narrows it: the value crosses Rust as an open
 * `Option<String>` and reaches JS through `invoke<SearchResponse>`, which
 * is an unchecked type assertion — so an unrecognised ordering (a `serve`
 * newer than this client, a third mode) would otherwise be laundered into
 * a field typed `SortMode` and reach the selector, where it matches
 * *neither* radio and leaves the control with nothing checked.
 *
 * An unknown value is therefore reported as **unknown**, which is the
 * degradation the absent-key case already has and already tests: show the
 * request, disable nothing, never a wrong claim. Collapsing the three
 * response-reading sites onto this one rule is also what stops them
 * drifting apart.
 */
export function asSortMode(value: unknown): SortMode | null {
  return value === "rank" || value === "date" ? value : null;
}

/** Why Relevance is disabled. Shown as the control's `title`. */
export const RELEVANCE_UNAVAILABLE_REASON =
  "These results have nothing to rank — filters alone are ordered by date. " +
  "Add search text and search again.";
