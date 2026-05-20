import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import SearchBar from "./SearchBar.svelte";
import { search } from "../lib/stores/search.svelte";

vi.mock("../lib/tauri", () => ({ runSearch: vi.fn(async () => ({
  results: [], next_cursor: null, total_estimate: null, took_ms: 0,
})) }));

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
});
