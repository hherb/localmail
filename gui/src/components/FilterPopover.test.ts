import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import FilterPopover from "./FilterPopover.svelte";
import { search } from "../lib/stores/search.svelte";

vi.mock("../lib/tauri", () => ({ runSearch: vi.fn(async () => ({
  results: [], next_cursor: null, total_estimate: null, took_ms: 0,
})) }));

afterEach(() => { search.reset(); vi.clearAllMocks(); });

describe("FilterPopover", () => {
  it("renders inputs for from/to/subject/after/before/has-attachment + Apply", () => {
    render(FilterPopover);
    expect(screen.getByLabelText(/from/i)).toBeTruthy();
    expect(screen.getByLabelText(/^to/i)).toBeTruthy();
    expect(screen.getByLabelText(/subject/i)).toBeTruthy();
    expect(screen.getByLabelText(/after/i)).toBeTruthy();
    expect(screen.getByLabelText(/before/i)).toBeTruthy();
    expect(screen.getByLabelText(/has attachment/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /apply/i })).toBeTruthy();
  });

  it("typing into from updates the local state then writes on Apply", async () => {
    render(FilterPopover);
    const fromInput = screen.getByLabelText(/from/i) as HTMLInputElement;
    await fireEvent.input(fromInput, { target: { value: "anna" } });
    await fireEvent.click(screen.getByRole("button", { name: /apply/i }));
    expect(search.snapshot.filters.from).toBe("anna");
  });

  it("Apply submits the search", async () => {
    render(FilterPopover);
    await fireEvent.click(screen.getByRole("button", { name: /apply/i }));
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
  });

  it("Clear resets the popover form", async () => {
    render(FilterPopover);
    const fromInput = screen.getByLabelText(/from/i) as HTMLInputElement;
    await fireEvent.input(fromInput, { target: { value: "anna" } });
    await fireEvent.click(screen.getByRole("button", { name: /clear/i }));
    expect((screen.getByLabelText(/from/i) as HTMLInputElement).value).toBe("");
  });
});
