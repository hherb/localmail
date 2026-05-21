/**
 * Search state singleton. Mirrors mail.svelte.ts in shape.
 *
 * The search store owns:
 *   query       — what's in the search bar (raw, may include DSL tokens)
 *   filters     — current structured filter state (popover + tree)
 *   results     — last SearchResponse rows
 *   tookMs      — last took_ms for "Search took 42 ms" caption
 *   loading     — true while the request is in flight
 *   errorMessage — surfaced from a failed submit
 *
 * submit() merges the DSL string with structured popover filters via
 * filtersUiToWire(). Tree-driven account/folder filters are written into
 * `filters.accountIds` / `filters.folderIds` by the caller before submit().
 */
import {
  emptyFilters,
  filtersUiToWire,
  type SearchFiltersUI,
  type SearchResultRow,
} from "../api/search";
import { runSearch } from "../tauri";
import { formatError } from "../format_error";
import { isSearchCursorExpired } from "../search_cursor_expired";

const DEFAULT_LIMIT = 50;

export type SortMode = "rank" | "date";

export interface SearchState {
  query: string;
  filters: SearchFiltersUI;
  // "rank" (default) orders results by hybrid-search relevance score; "date"
  // keeps the same hybrid candidate pool but orders the page by
  // COALESCE(internal_date, date_sent) DESC — matches the server's
  // /v1/search `sort` parameter.
  sort: SortMode;
  results: SearchResultRow[];
  cursor: string | null;
  hasMore: boolean;
  tookMs: number | null;
  loading: boolean;
  loadingMore: boolean;
  errorMessage: string | null;
}

function initialState(): SearchState {
  return {
    query: "",
    filters: emptyFilters(),
    sort: "rank",
    results: [],
    cursor: null,
    hasMore: false,
    tookMs: null,
    loading: false,
    loadingMore: false,
    errorMessage: null,
  };
}

class SearchStore {
  #state: SearchState = $state(initialState());
  // Monotonic submit counter. Each submit() captures the next value and
  // ignores its response if a later submit has run in the meantime — without
  // this guard a slower first response would clobber a newer second one.
  #submitSeq = 0;

  get snapshot(): SearchState { return this.#state; }

  setQuery(q: string): void { this.#state.query = q; }

  setFilters(f: SearchFiltersUI): void { this.#state.filters = f; }

  setSort(s: SortMode): void { this.#state.sort = s; }

  reset(): void {
    this.#state = initialState();
    this.#submitSeq++;
  }

  /**
   * Returns true when the store has no scoping signal — no free-text query,
   * no chip filters, no account/folder narrowing. In that state, submit()
   * would fall through to a vector-arm-only retrieval against the embedding
   * of the empty string, which produces ~`rerank_pool_size` arbitrary hits.
   * Callers that just cleared the user's last filter should call `reset()`
   * instead, which flips MessageList back to the recent-mail view.
   */
  hasNoScope(): boolean {
    const s = this.#state;
    if (s.query.trim() !== "") return false;
    const f = s.filters;
    if (f.accountIds.length > 0 || f.folderIds.length > 0) return false;
    if (f.from || f.to || f.subject || f.after || f.before) return false;
    if (f.hasAttachment === true) return false;
    return true;
  }

  async submit(): Promise<void> {
    const seq = ++this.#submitSeq;
    this.#state.loading = true;
    this.#state.errorMessage = null;
    try {
      const resp = await runSearch({
        query: this.#state.query,
        filters: filtersUiToWire(this.#state.filters),
        limit: DEFAULT_LIMIT,
        cursor: null,
        sort: this.#state.sort,
      });
      if (seq !== this.#submitSeq) return;
      this.#state.results = resp.results;
      this.#state.tookMs = resp.took_ms;
      this.#state.cursor = resp.next_cursor;
      this.#state.hasMore = resp.next_cursor !== null;
    } catch (err: unknown) {
      if (seq !== this.#submitSeq) return;
      // Clear stale results so the UI does not show prior query's matches
      // alongside the new error banner; searchActive (tookMs !== null) flips
      // off so MessageList reverts to its non-search rendering path.
      this.#state.results = [];
      this.#state.tookMs = null;
      this.#state.cursor = null;
      this.#state.hasMore = false;
      this.#state.errorMessage = formatError(err);
    } finally {
      if (seq === this.#submitSeq) this.#state.loading = false;
    }
  }

  async loadMore(): Promise<void> {
    if (!this.#state.hasMore || this.#state.cursor === null) return;
    if (this.#state.loadingMore) return;
    const seq = this.#submitSeq;
    const cursor = this.#state.cursor;
    const priorCount = this.#state.results.length;
    this.#state.loadingMore = true;
    try {
      let resp;
      try {
        resp = await runSearch({
          query: this.#state.query,
          filters: filtersUiToWire(this.#state.filters),
          limit: DEFAULT_LIMIT,
          cursor,
          sort: this.#state.sort,
        });
      } catch (err: unknown) {
        if (!isSearchCursorExpired(err)) throw err;
        // Transparent recovery: re-submit without cursor, drop the rows
        // the user already has, append the remainder.
        const fresh = await runSearch({
          query: this.#state.query,
          filters: filtersUiToWire(this.#state.filters),
          limit: DEFAULT_LIMIT,
          cursor: null,
          sort: this.#state.sort,
        });
        if (seq !== this.#submitSeq) return;
        if (fresh.results.length <= priorCount) {
          // New pool is smaller — full reset.
          this.#state.results = fresh.results;
        } else {
          this.#state.results = [
            ...this.#state.results,
            ...fresh.results.slice(priorCount),
          ];
        }
        this.#state.cursor = fresh.next_cursor;
        this.#state.hasMore = fresh.next_cursor !== null;
        return;
      }
      if (seq !== this.#submitSeq) return;
      this.#state.results = [...this.#state.results, ...resp.results];
      this.#state.cursor = resp.next_cursor;
      this.#state.hasMore = resp.next_cursor !== null;
    } catch (err: unknown) {
      if (seq !== this.#submitSeq) return;
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loadingMore = false;
    }
  }
}

// formatError moved to ../format_error.ts

export const search = new SearchStore();

// Exported for tests only — populates results + tookMs without firing runSearch.
// `as unknown as { snapshot: SearchState }` is intentional: it bypasses the
// readonly `snapshot` getter to allow direct mutation. Production code MUST
// only mutate state via setQuery/setFilters/submit/reset.
export function __setSearchResultsForTest(
  results: SearchResultRow[],
  tookMs: number,
): void {
  const s = search as unknown as { snapshot: SearchState };
  s.snapshot.results = results;
  s.snapshot.tookMs = tookMs;
}
