/**
 * The pure rules the sort selector renders and reacts by.
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
 * The server now reports its resolution as `sort_applied` and, since #353,
 * whether ranking was possible at all as `rankable`. These rules turn the
 * two into what the selector shows, what it offers, and what a click on it
 * means. They live together because those are three readings of the same
 * pair of fields — the call `search_paging.ts` makes for the paging pair.
 *
 * `rankable` is a separate field rather than an inference because
 * `sort_applied` cannot answer it: a `date` the user chose and a `date`
 * imposed on a textless query are the same value there. Inferring it worked
 * only while the client could not change the request, which stopped being
 * true the moment a click was recorded (#353).
 *
 * No rule here inspects the query. That is deliberate and is the same
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
 * Read straight off `rankable` (#353). It used to be *inferred* — `applied
 * === "date" && requested === "rank"` — which was the best available reading
 * while the server reported only the ordering, and wrong in two ways once a
 * click could change the request. It left Relevance enabled after an
 * explicit Date selection on a textless query (a documented imprecision),
 * and it meant recording that click would **re-enable Relevance on a query
 * that genuinely cannot be ranked**. Rankability is a property of the query,
 * so nothing the user prefers can move it, and both faults go with the
 * inference.
 *
 * `null` is unknown — nothing has run, or a `serve` predating the field —
 * and disables nothing. Never claiming unavailability without proof is the
 * same degradation `asSortMode` gives an unrecognised ordering.
 *
 * The flag describes the rows **on screen**, not the query box: `rankable`
 * deliberately outlives an edit to the box (see `displayedSort`), so after a
 * textless search, typing rankable text leaves Relevance disabled until the
 * next submit. That is why `RELEVANCE_UNAVAILABLE_REASON` is worded after
 * the rows rather than as an instruction about the box.
 */
export function relevanceUnavailable(rankable: boolean | null): boolean {
  return rankable === false;
}

/**
 * Whether a response says ranking was possible, or `null` when it says
 * nothing.
 *
 * The one place the wire value becomes a boolean, for the reason
 * `asSortMode` is the one place the ordering becomes a `SortMode`: the value
 * crosses Rust as an open `Option<bool>` and reaches JS through
 * `invoke<SearchResponse>`, an **unchecked** type assertion.
 *
 * A truthiness test would be wrong in both directions on what an unchecked
 * cast can deliver — the string `"false"` is truthy and the number `0` is
 * falsy — and the second silently disables a working control. An unknown
 * value degrades to `null`, which claims nothing.
 */
export function asRankable(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** What a click on one of the sort radios should do. */
export interface SortClickOutcome {
  /** Store it as the user's preference — it is not what they had. */
  record: boolean;
  /** Re-run the search — the ordering on screen is not what they clicked. */
  resubmit: boolean;
}

/**
 * The two independent questions a click asks, answered from two fields.
 *
 * They were one question read off one field, and #353 is the gap that
 * opened when `displayedSort` made the two fields disagree. The radios
 * render the ordering that *ran* while the guard compared the stored
 * *preference*, so in the state #345 introduces — preference `rank`, shown
 * `date` — the checked radio is Date and clicking it fires no `change`
 * event at all. The user affirms Date, nothing is recorded, and their next
 * text search comes back rank-ordered under a control that said Date.
 *
 * Recording and re-running are genuinely separate: a click that agrees with
 * the rows on screen but not with the stored preference must be *recorded*
 * without re-running (the rows would not change, so the request would be a
 * wasted round trip), and the reverse pairing is reachable too as the
 * transient between a click and its response landing.
 *
 * Taken as an object rather than three positional `SortMode`s, for the
 * reason `statedSort` shed its third parameter: three same-typed positional
 * arguments make a transposition type-check.
 */
export function sortClick(
  { preference, shown, clicked }: {
    preference: SortMode;
    shown: SortMode;
    clicked: SortMode;
  },
): SortClickOutcome {
  return { record: preference !== clicked, resubmit: shown !== clicked };
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
