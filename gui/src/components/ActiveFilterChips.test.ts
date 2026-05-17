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

  it("clicking a chip's × removes that filter and re-submits", async () => {
    search.setFilters({
      accountIds: [], folderIds: [],
      from: "anna", to: "", subject: "", after: "", before: "", hasAttachment: null,
    });
    render(ActiveFilterChips);
    await fireEvent.click(screen.getByRole("button", { name: /remove from/i }));
    expect(search.snapshot.filters.from).toBe("");
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
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
