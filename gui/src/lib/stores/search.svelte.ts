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

const DEFAULT_LIMIT = 50;

export interface SearchState {
  query: string;
  filters: SearchFiltersUI;
  results: SearchResultRow[];
  tookMs: number | null;
  loading: boolean;
  errorMessage: string | null;
}

function initialState(): SearchState {
  return {
    query: "",
    filters: emptyFilters(),
    results: [],
    tookMs: null,
    loading: false,
    errorMessage: null,
  };
}

class SearchStore {
  #state: SearchState = $state(initialState());

  get snapshot(): SearchState { return this.#state; }

  setQuery(q: string): void { this.#state.query = q; }

  setFilters(f: SearchFiltersUI): void { this.#state.filters = f; }

  reset(): void { this.#state = initialState(); }

  async submit(): Promise<void> {
    this.#state.loading = true;
    this.#state.errorMessage = null;
    try {
      const resp = await runSearch({
        query: this.#state.query,
        filters: filtersUiToWire(this.#state.filters),
        limit: DEFAULT_LIMIT,
        cursor: null,
      });
      this.#state.results = resp.results;
      this.#state.tookMs = resp.took_ms;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loading = false;
    }
  }
}

function formatError(err: unknown): string {
  if (err && typeof err === "object") {
    const o = err as { kind?: string; detail?: unknown };
    if (o.kind && o.detail !== undefined) {
      const detailStr =
        typeof o.detail === "object" && o.detail !== null
          ? formatError(o.detail)
          : String(o.detail);
      return `${o.kind}: ${detailStr}`;
    }
    if (o.kind) return String(o.kind);
  }
  return String(err);
}

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
