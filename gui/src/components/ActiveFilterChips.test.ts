import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import ActiveFilterChips from "./ActiveFilterChips.svelte";
import { search } from "../lib/stores/search.svelte";

vi.mock("../lib/tauri", () => ({ runSearch: vi.fn(async () => ({
  results: [], next_cursor: null, total_estimate: null, took_ms: 0,
})) }));

afterEach(() => { search.reset(); vi.clearAllMocks(); });

describe("ActiveFilterChips", () => {
  it("renders nothing when no filters are set", () => {
    const { container } = render(ActiveFilterChips);
    expect(container.querySelectorAll(".chip").length).toBe(0);
  });

  it("renders a chip for each non-empty popover filter", () => {
    search.setFilters({
      accountIds: [], folderIds: [],
      from: "anna", to: "", subject: "trip",
      after: "2024-01-01", before: "", hasAttachment: true,
    });
    render(ActiveFilterChips);
    expect(screen.getByText(/from: anna/i)).toBeTruthy();
    expect(screen.getByText(/subject: trip/i)).toBeTruthy();
    expect(screen.getByText(/after: 2024-01-01/i)).toBeTruthy();
    expect(screen.getByText(/has attachment/i)).toBeTruthy();
  });

  it("clicking a chip's × removes that filter and re-submits when scope remains", async () => {
    // A remaining accountIds scope means there's still something narrower
    // than "all recent mail" to search for, so re-submitting is correct.
    search.setQuery("hello");
    search.setFilters({
      accountIds: ["1"], folderIds: [],
      from: "anna", to: "", subject: "", after: "", before: "", hasAttachment: null,
    });
    render(ActiveFilterChips);
    await fireEvent.click(screen.getByRole("button", { name: /remove from/i }));
    expect(search.snapshot.filters.from).toBe("");
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
  });

  it.each([
    ["after", "dateFrom"],
    ["before", "dateTo"],
  ] as const)("removing the %s chip also clears its %s twin", async (key, twin) => {
    // The popover writes a date to both fields and seeds itself from the
    // twin, so a twin left behind re-applied a removed date on the next Apply
    // — and kept hasNoScope() false, so the last chip's × never reset.
    search.setQuery("hello");
    search.setFilters({
      accountIds: [], folderIds: [],
      from: "", to: "", subject: "", after: "", before: "", hasAttachment: null,
      [key]: "2024-01-01", [twin]: "2024-01-01",
    });
    render(ActiveFilterChips);
    await fireEvent.click(screen.getByRole("button", { name: new RegExp(`remove ${key}`, "i") }));
    expect(search.snapshot.filters[key]).toBe("");
    expect(search.snapshot.filters[twin]).toBe("");
  });

  it("removing the only date chip resets, its twin no longer counting as scope", async () => {
    search.setFilters({
      accountIds: [], folderIds: [],
      from: "", to: "", subject: "", after: "2024-01-01", before: "", hasAttachment: null,
      dateFrom: "2024-01-01",
    });
    const { __setSearchResultsForTest } = await import("../lib/stores/search.svelte");
    __setSearchResultsForTest([], 12);
    render(ActiveFilterChips);
    await fireEvent.click(screen.getByRole("button", { name: /remove after/i }));
    expect(search.snapshot.tookMs).toBeNull();
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).not.toHaveBeenCalled();
  });

  it("clicking the last chip's × with no remaining scope resets instead of submitting", async () => {
    // Empty-query search degenerates to vector-arm hits against the
    // embedding of the empty string -> exactly `rerank_pool_size` (default
    // 20) arbitrary results. When the user clears their final scoping
    // signal, fall back to the recent-mail view (mail.messages) rather than
    // run a meaningless search.
    search.setFilters({
      accountIds: [], folderIds: [],
      from: "anna", to: "", subject: "", after: "", before: "", hasAttachment: null,
    });
    // Seed tookMs so we can assert reset() was the path taken.
    const { __setSearchResultsForTest } = await import("../lib/stores/search.svelte");
    __setSearchResultsForTest([], 12);
    render(ActiveFilterChips);
    await fireEvent.click(screen.getByRole("button", { name: /remove from/i }));
    expect(search.snapshot.filters.from).toBe("");
    expect(search.snapshot.tookMs).toBeNull();
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).not.toHaveBeenCalled();
  });

  it("does not show chips for accountIds/folderIds (tree owns those)", () => {
    search.setFilters({
      accountIds: ["1", "3"], folderIds: ["5"],
      from: "", to: "", subject: "", after: "", before: "", hasAttachment: null,
    });
    const { container } = render(ActiveFilterChips);
    expect(container.querySelectorAll(".chip").length).toBe(0);
  });
});
