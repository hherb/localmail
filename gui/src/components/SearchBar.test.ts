import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import SearchBar from "./SearchBar.svelte";
import { search } from "../lib/stores/search.svelte";

vi.mock("../lib/tauri", () => ({ runSearch: vi.fn(async () => ({
  results: [], next_cursor: null, total_estimate: null, took_ms: 0,
})) }));

/** Make the next search come back reporting `sortApplied` as the ordering. */
async function serveWith(sortApplied: "rank" | "date"): Promise<void> {
  const { runSearch } = await import("../lib/tauri");
  (runSearch as ReturnType<typeof vi.fn>).mockResolvedValue({
    results: [], next_cursor: null, total_estimate: null, took_ms: 1,
    sort_applied: sortApplied,
  });
}

afterEach(() => { search.reset(); vi.clearAllMocks(); });

describe("SearchBar", () => {
  it("renders an input and a Filters button", () => {
    render(SearchBar);
    expect(screen.getByPlaceholderText(/search/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /filters/i })).toBeTruthy();
  });

  it("typing updates search.query", async () => {
    render(SearchBar);
    const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: "hello" } });
    expect(search.snapshot.query).toBe("hello");
  });

  it("submitting via Enter calls search.submit()", async () => {
    render(SearchBar);
    const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: "hi" } });
    await fireEvent.keyDown(input, { key: "Enter" });
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
  });

  it("clicking the search button also submits", async () => {
    render(SearchBar);
    const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: "hi" } });
    await fireEvent.click(screen.getByRole("button", { name: /^search$/i }));
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
  });

  it("toggling the Filters button opens then closes the popover", async () => {
    const { container } = render(SearchBar);
    const filtersBtn = screen.getByRole("button", { name: /filters/i });
    expect(container.querySelector('[role="dialog"]')).toBeFalsy();
    await fireEvent.click(filtersBtn);
    expect(container.querySelector('[role="dialog"]')).toBeTruthy();
    await fireEvent.click(filtersBtn);
    expect(container.querySelector('[role="dialog"]')).toBeFalsy();
  });

  it("pressing Escape closes the popover when open", async () => {
    const { container } = render(SearchBar);
    await fireEvent.click(screen.getByRole("button", { name: /filters/i }));
    expect(container.querySelector('[role="dialog"]')).toBeTruthy();
    await fireEvent.keyDown(window, { key: "Escape" });
    expect(container.querySelector('[role="dialog"]')).toBeFalsy();
  });

  it("renders Relevance/Date radios; Relevance is selected by default", () => {
    render(SearchBar);
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    const date = screen.getByRole("radio", { name: /^date$/i }) as HTMLInputElement;
    expect(relevance.checked).toBe(true);
    expect(date.checked).toBe(false);
  });

  it("clicking the Date radio updates store.sort", async () => {
    render(SearchBar);
    await fireEvent.click(screen.getByRole("radio", { name: /^date$/i }));
    expect(search.snapshot.sort).toBe("date");
  });

  it("changing sort re-submits when a search is already active", async () => {
    // Seed an active search state so the toggle's "re-run on change" path fires.
    const { __setSearchResultsForTest } = await import("../lib/stores/search.svelte");
    __setSearchResultsForTest([], 12);
    search.setQuery("hello");
    render(SearchBar);
    await fireEvent.click(screen.getByRole("radio", { name: /^date$/i }));
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
    const arg = (runSearch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(arg.sort).toBe("date");
  });

  it("changing sort with no active search does NOT submit", async () => {
    // tookMs === null → no search is active; toggle should just store the
    // preference for the user's next submit, not fire a request.
    render(SearchBar);
    await fireEvent.click(screen.getByRole("radio", { name: /^date$/i }));
    expect(search.snapshot.sort).toBe("date");
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).not.toHaveBeenCalled();
  });

  it("Enter-submitting sends the current sort to runSearch", async () => {
    search.setQuery("hi");
    render(SearchBar);
    await fireEvent.click(screen.getByRole("radio", { name: /^date$/i }));
    const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    await fireEvent.keyDown(input, { key: "Enter" });
    const { runSearch } = await import("../lib/tauri");
    const arg = (runSearch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0];
    expect(arg.sort).toBe("date");
  });

  // ---------------------------------------------------------------------
  // #345 — the selector shows the ordering that ran, not the request.
  // ---------------------------------------------------------------------

  it("reflects date order after a textless search, and says why", async () => {
    // The defect: `from:alice` is textless to the server, so it is served
    // date-ordered while the radio asserted Relevance.
    await serveWith("date");
    search.setQuery("from:alice");
    render(SearchBar);
    await search.submit();
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    const date = screen.getByRole("radio", { name: /^date$/i }) as HTMLInputElement;
    expect(date.checked).toBe(true);
    expect(relevance.checked).toBe(false);
    expect(relevance.disabled).toBe(true);
    // Known gap, recorded rather than left silent: reverting the *Relevance*
    // input's `checked` binding alone to `snapshot.sort` survives every
    // assertion here, because radio-group exclusivity resolves the conflict
    // in the Date input's favour and the rendered state is identical.
    // Reverting the Date binding, or both, is caught (mutation-checked).
    // What carries the one-sided case is `relevance.disabled` below, which
    // no group semantics can mask.
    // Disabled without a reason is just a quieter inert control.
    expect(relevance.closest("label")?.title ?? "").toMatch(/search text/i);
  });

  it("leaves Relevance selected and enabled when the server ranked", async () => {
    await serveWith("rank");
    search.setQuery("invoice");
    render(SearchBar);
    await search.submit();
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    expect(relevance.checked).toBe(true);
    expect(relevance.disabled).toBe(false);
  });

  it("keeps Relevance available after an explicit Date search", async () => {
    // A date *request* proves nothing about whether rank was available, so
    // the control must not be disabled on the strength of it.
    await serveWith("date");
    search.setQuery("invoice");
    search.setSort("date");
    render(SearchBar);
    await search.submit();
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    expect(relevance.disabled).toBe(false);
  });

  it("shows the stored preference before anything has run", () => {
    search.setSort("date");
    render(SearchBar);
    const date = screen.getByRole("radio", { name: /^date$/i }) as HTMLInputElement;
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    expect(date.checked).toBe(true);
    expect(relevance.disabled).toBe(false);
  });

  it("falls back to the request when the server reports no ordering", async () => {
    // An older `serve` omits the key. Showing the request is the pre-#345
    // behaviour, which is the right degradation — never a wrong claim.
    const { runSearch } = await import("../lib/tauri");
    (runSearch as ReturnType<typeof vi.fn>).mockResolvedValue({
      results: [], next_cursor: null, total_estimate: null, took_ms: 1,
    });
    search.setQuery("from:alice");
    render(SearchBar);
    await search.submit();
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    expect(relevance.checked).toBe(true);
    expect(relevance.disabled).toBe(false);
  });
});
