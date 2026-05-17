/**
 * Wire-shape types for /v1/search. Mirrors the Rust structs in
 * src-tauri/src/commands/search.rs.
 */
export interface SearchFiltersWire {
  account_ids: string[];
  folder_ids: string[];
  from: string | null;
  to: string | null;
  subject: string | null;
  after: string | null;   // YYYY-MM-DD
  before: string | null;  // YYYY-MM-DD
  has_attachment: boolean | null;
}

export interface SearchRequest {
  query: string;
  filters: SearchFiltersWire;
  limit: number;
  cursor: string | null;
}

export interface SearchAddress {
  address: string | null;
  name: string | null;
}

export interface SearchAccount {
  id: string;
  name: string | null;
}

export interface SearchFolder {
  id: string;
  full_path: string;
}

export interface SearchResultRow {
  message_id: string;
  account: SearchAccount;
  folder: SearchFolder | null;
  subject: string | null;
  from: SearchAddress;
  to: SearchAddress[];
  date: string | null;
  snippet_html: string | null;
  has_attachments: boolean;
  score: number;
  matched_arms: string[];
}

export interface SearchResponse {
  results: SearchResultRow[];
  next_cursor: string | null;
  total_estimate: number | null;
  took_ms: number;
}

/**
 * UI-facing filter shape. Distinct from `SearchFiltersWire` because the UI
 * uses idiomatic empty-strings / undefineds, while the wire shape uses
 * empty arrays / nulls that the Rust struct can omit via skip_serializing_if.
 */
export interface SearchFiltersUI {
  accountIds: string[];
  folderIds: string[];
  from: string;
  to: string;
  subject: string;
  after: string;
  before: string;
  hasAttachment: boolean | null;  // null = "don't care", true/false explicit
}

export function emptyFilters(): SearchFiltersUI {
  return {
    accountIds: [], folderIds: [],
    from: "", to: "", subject: "", after: "", before: "",
    hasAttachment: null,
  };
}

export function filtersUiToWire(ui: SearchFiltersUI): SearchFiltersWire {
  return {
    account_ids: ui.accountIds,
    folder_ids: ui.folderIds,
    from: ui.from || null,
    to: ui.to || null,
    subject: ui.subject || null,
    after: ui.after || null,
    before: ui.before || null,
    has_attachment: ui.hasAttachment,
  };
}
