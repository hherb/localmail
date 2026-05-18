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
    expect(screen.getByLabelText(/^from$/i)).toBeTruthy();
    expect(screen.getByLabelText(/^to$/i)).toBeTruthy();
    expect(screen.getByLabelText(/subject/i)).toBeTruthy();
    expect(screen.getByLabelText(/^after$/i)).toBeTruthy();
    expect(screen.getByLabelText(/^before$/i)).toBeTruthy();
    expect(screen.getByLabelText(/has attachment/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /apply/i })).toBeTruthy();
  });

  it("typing into from updates the local state then writes on Apply", async () => {
    render(FilterPopover);
    const fromInput = screen.getByLabelText(/^from$/i) as HTMLInputElement;
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
    const fromInput = screen.getByLabelText(/^from$/i) as HTMLInputElement;
    await fireEvent.input(fromInput, { target: { value: "anna" } });
    await fireEvent.click(screen.getByRole("button", { name: /^clear$/i }));
    expect((screen.getByLabelText(/^from$/i) as HTMLInputElement).value).toBe("");
  });

  it("filling dateFrom propagates to the search store on Apply", async () => {
    render(FilterPopover);
    const dateFromInput = screen.getByLabelText(/^from date$/i) as HTMLInputElement;
    await fireEvent.input(dateFromInput, { target: { value: "2024-01-15" } });
    await fireEvent.click(screen.getByRole("button", { name: /apply/i }));
    expect(search.snapshot.filters.dateFrom).toBe("2024-01-15");
  });

  it("filling dateTo propagates to the search store on Apply", async () => {
    render(FilterPopover);
    const dateToInput = screen.getByLabelText(/^to date$/i) as HTMLInputElement;
    await fireEvent.input(dateToInput, { target: { value: "2024-12-31" } });
    await fireEvent.click(screen.getByRole("button", { name: /apply/i }));
    expect(search.snapshot.filters.dateTo).toBe("2024-12-31");
  });

  it("filling language lowercases the value", async () => {
    render(FilterPopover);
    const langInput = screen.getByLabelText(/^language$/i) as HTMLInputElement;
    await fireEvent.input(langInput, { target: { value: "EN" } });
    await fireEvent.click(screen.getByRole("button", { name: /apply/i }));
    expect(search.snapshot.filters.language).toBe("en");
  });

  it("× button next to language clears just the language field", async () => {
    render(FilterPopover);
    const langInput = screen.getByLabelText(/^language$/i) as HTMLInputElement;
    await fireEvent.input(langInput, { target: { value: "en" } });
    await fireEvent.click(screen.getByRole("button", { name: /clear language/i }));
    expect((screen.getByLabelText(/^language$/i) as HTMLInputElement).value).toBe("");
  });

  it("Apply invokes the onClose callback", async () => {
    const onClose = vi.fn();
    render(FilterPopover, { props: { onClose } });
    await fireEvent.click(screen.getByRole("button", { name: /apply/i }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("header × button invokes onClose without applying or submitting", async () => {
    const onClose = vi.fn();
    render(FilterPopover, { props: { onClose } });
    const fromInput = screen.getByLabelText(/^from$/i) as HTMLInputElement;
    await fireEvent.input(fromInput, { target: { value: "anna" } });
    await fireEvent.click(screen.getByRole("button", { name: /close filters/i }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(search.snapshot.filters.from).toBe("");
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).not.toHaveBeenCalled();
  });
});
