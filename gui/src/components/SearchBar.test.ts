import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tick } from "svelte";
import SearchBar from "./SearchBar.svelte";
import { search } from "../lib/stores/search.svelte";

vi.mock("../lib/tauri", () => ({ runSearch: vi.fn(async () => ({
  results: [], next_cursor: null, total_estimate: null, took_ms: 0,
})) }));

/**
 * Make the next search report `sortApplied` as the ordering that ran and
 * `rankable` as whether it could have ranked at all (#353).
 *
 * `rankable` defaults to `true` — "rank was available" — because that is
 * the value which disables nothing, so a test about ordering alone is not
 * silently also a test about availability.
 */
async function serveWith(
  sortApplied: "rank" | "date",
  rankable = true,
): Promise<void> {
  const { runSearch } = await import("../lib/tauri");
  (runSearch as ReturnType<typeof vi.fn>).mockResolvedValue({
    results: [], next_cursor: null, total_estimate: null, took_ms: 1,
    sort_applied: sortApplied, rankable,
  });
}

/** The visible reason, if the component is rendering one (#354). */
function reasonText(): string | null {
  const el = document.querySelector("[data-testid='relevance-unavailable']");
  return el ? (el.textContent ?? "") : null;
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
    await serveWith("date", false);
    search.setQuery("from:alice");
    render(SearchBar);
    await search.submit();
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    const date = screen.getByRole("radio", { name: /^date$/i }) as HTMLInputElement;
    expect(date.checked).toBe(true);
    expect(relevance.checked).toBe(false);
    expect(relevance.disabled).toBe(true);
    // In THIS state a reverted Relevance `checked` binding is masked:
    // radio-group exclusivity resolves the conflict in Date's favour, so
    // the rendered result is identical. It is caught in the reversed state
    // instead — see "keeps showing what ran while a newer Date request is
    // in flight" below, where reading the request leaves both radios
    // unchecked. (The PR recorded this mutation as surviving outright; it
    // survives only this direction.) Reverting the Date binding, or both,
    // is caught here. Disabled without a reason is a quieter inert control,
    // so the reason is asserted too — now as text in the markup rather
    // than a `title`, which #354 showed is unreachable by keyboard (a
    // disabled input leaves the tab order) and announced inconsistently.
    expect(reasonText() ?? "").toMatch(/search text/i);
  });

  it("keeps showing what ran while a newer Date request is in flight", async () => {
    // This is the state the PR recorded as unpinnable, and it is only
    // unpinnable in the (rank, date) direction: there, radio-group
    // exclusivity resolves the conflict in Date's favour so a reverted
    // Relevance binding renders identically. Reversed, a binding that reads
    // the request leaves BOTH radios unchecked, which `checked` can see.
    //
    // Real state, not a contrivance: `onSortChange` calls `setSort` and
    // then awaits `submit()`, so between a Date click and its response the
    // component renders requested="date" over applied="rank".
    await serveWith("rank");
    search.setQuery("invoice");
    render(SearchBar);
    await search.submit();
    search.setSort("date");
    await tick();
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    const date = screen.getByRole("radio", { name: /^date$/i }) as HTMLInputElement;
    expect(relevance.checked).toBe(true);
    expect(date.checked).toBe(false);
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
    // Unchanged outcome, and since #353 it holds for the right reason: the
    // server reports this query rankable, so a date *request* cannot
    // disable the control. It used to rest on the request not being
    // "rank" — which is what recording a Date click then broke.
    await serveWith("date", true);
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

  // ---------------------------------------------------------------------
  // #353 — a click on the selector records a preference, always.
  // ---------------------------------------------------------------------

  it("records a click on the already-checked Date radio", async () => {
    // The reported defect. The radios show what *ran*, so after a textless
    // search Date is checked while the stored preference is still `rank`.
    // Clicking it fires no `change` event, so the preference was never
    // recorded — and the user's next text search came back rank-ordered
    // under a control that said Date.
    await serveWith("date", false);
    search.setQuery("from:alice");
    render(SearchBar);
    await search.submit();
    const date = screen.getByRole("radio", { name: /^date$/i }) as HTMLInputElement;
    expect(date.checked).toBe(true);
    expect(search.snapshot.sort).toBe("rank");
    await fireEvent.click(date);
    expect(search.snapshot.sort).toBe("date");
  });

  it("does not re-run the search when only the preference moved", async () => {
    // The rows on screen are already date-ordered, so re-running would be a
    // wasted round trip. Recording and re-running are separate questions.
    await serveWith("date", false);
    search.setQuery("from:alice");
    render(SearchBar);
    await search.submit();
    const { runSearch } = await import("../lib/tauri");
    (runSearch as ReturnType<typeof vi.fn>).mockClear();
    await fireEvent.click(screen.getByRole("radio", { name: /^date$/i }));
    expect(runSearch).not.toHaveBeenCalled();
  });

  it("recording that click does NOT re-enable Relevance", async () => {
    // The reason #353 needed `rankable` rather than only an `onclick`.
    // While availability was inferred from the request, recording the click
    // made the request "date" and flipped the inference — re-enabling
    // Relevance on a query that genuinely cannot be ranked. Rankability is
    // a property of the query, so the preference cannot move it.
    await serveWith("date", false);
    search.setQuery("from:alice");
    render(SearchBar);
    await search.submit();
    await fireEvent.click(screen.getByRole("radio", { name: /^date$/i }));
    await tick();
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    expect(relevance.disabled).toBe(true);
    expect(reasonText() ?? "").toMatch(/search text/i);
  });

  it("still records and re-runs an ordinary change of mind", async () => {
    // The positive control: the pre-existing behaviour must survive, or the
    // fix above has merely broken the selector a different way.
    await serveWith("rank", true);
    search.setQuery("invoice");
    render(SearchBar);
    await search.submit();
    const { runSearch } = await import("../lib/tauri");
    (runSearch as ReturnType<typeof vi.fn>).mockClear();
    await fireEvent.click(screen.getByRole("radio", { name: /^date$/i }));
    expect(search.snapshot.sort).toBe("date");
    expect(runSearch).toHaveBeenCalledOnce();
  });

  it("re-runs exactly once for a real change, not twice", async () => {
    // Only `click` is bound, and this is why. A radio that actually changes
    // fires `change` *and* `click`, so binding both double-fired a real
    // change of mind — `shownSort` only moves when the response lands, so
    // the second handler still saw a disagreement and submitted again. A
    // radio already checked fires no `change` at all, which is the #353
    // state, so `click` is the strict superset and the right one to bind.
    await serveWith("rank", true);
    search.setQuery("invoice");
    render(SearchBar);
    await search.submit();
    const { runSearch } = await import("../lib/tauri");
    (runSearch as ReturnType<typeof vi.fn>).mockClear();
    const date = screen.getByRole("radio", { name: /^date$/i }) as HTMLInputElement;
    await fireEvent.click(date);
    await fireEvent.change(date);
    expect(runSearch).toHaveBeenCalledOnce();
  });

  // ---------------------------------------------------------------------
  // #354 — the disable reason is reachable, not hidden in a tooltip.
  // ---------------------------------------------------------------------

  it("renders the reason as text and associates it with the control", async () => {
    // A `title` is hover-only for pointer users and announced
    // inconsistently by screen readers — and a disabled input is out of the
    // tab order, so it could not be reached by keyboard at all. Visible
    // text is the precedent this codebase already sets for a
    // server-disabled control (AccountForm's `.hint`, DaemonPanel's
    // `.note`); `aria-describedby` makes the association explicit.
    await serveWith("date", false);
    search.setQuery("from:alice");
    render(SearchBar);
    await search.submit();
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    const id = relevance.getAttribute("aria-describedby");
    expect(id).toBeTruthy();
    const note = document.getElementById(id as string);
    expect(note).toBeTruthy();
    expect(note?.textContent ?? "").toMatch(/search text/i);
    // And it is no longer only a tooltip.
    expect(relevance.closest("label")?.getAttribute("title")).toBe(null);
  });

  it("renders no reason while Relevance is available", async () => {
    await serveWith("rank", true);
    search.setQuery("invoice");
    render(SearchBar);
    await search.submit();
    const relevance = screen.getByRole("radio", { name: /relevance/i }) as HTMLInputElement;
    expect(reasonText()).toBe(null);
    expect(relevance.getAttribute("aria-describedby")).toBe(null);
  });
});
