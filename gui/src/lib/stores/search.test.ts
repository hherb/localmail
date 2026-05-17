import { afterEach, describe, expect, it, vi } from "vitest";
import { search } from "./search.svelte";
import { emptyFilters } from "../api/search";

vi.mock("../tauri", () => ({
  runSearch: vi.fn(),
}));

import { runSearch } from "../tauri";

afterEach(() => {
  search.reset();
  vi.clearAllMocks();
});

describe("search store", () => {
  it("starts empty / idle", () => {
    expect(search.snapshot.query).toBe("");
    expect(search.snapshot.results).toEqual([]);
    expect(search.snapshot.tookMs).toBe(null);
    expect(search.snapshot.loading).toBe(false);
    expect(search.snapshot.errorMessage).toBe(null);
  });

  it("setQuery + setFilters update state without firing a request", () => {
    search.setQuery("hello");
    search.setFilters({ ...emptyFilters(), from: "anna" });
    expect(search.snapshot.query).toBe("hello");
    expect(search.snapshot.filters.from).toBe("anna");
    expect(runSearch).not.toHaveBeenCalled();
  });

  it("submit() calls runSearch with the merged DSL+structured payload", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue({
        results: [{ message_id: "1", account: { id: "1", name: null },
                    folder: null, subject: "Hi", from: { address: null, name: null },
                    to: [], date: null, snippet_html: "x", has_attachments: false,
                    score: 0.5, matched_arms: ["bm25"] }],
        next_cursor: null, total_estimate: null, took_ms: 42.0,
      });
    search.setQuery("hello");
    await search.submit();
    expect(runSearch).toHaveBeenCalledTimes(1);
    expect(search.snapshot.results.length).toBe(1);
    expect(search.snapshot.tookMs).toBe(42.0);
    expect(search.snapshot.loading).toBe(false);
  });

  it("submit() failure surfaces errorMessage and clears loading", async () => {
    (runSearch as unknown as { mockRejectedValue: (v: unknown) => void })
      .mockRejectedValue({ kind: "HttpError", detail: { status: 500 } });
    search.setQuery("hi");
    await search.submit();
    expect(search.snapshot.loading).toBe(false);
    expect(search.snapshot.errorMessage).toContain("HttpError");
    expect(search.snapshot.results).toEqual([]);
  });

  it("submit() failure resets tookMs so MessageList exits search mode", async () => {
    (runSearch as unknown as { mockResolvedValueOnce: (v: unknown) => void })
      .mockResolvedValueOnce({
        results: [{ message_id: "1", account: { id: "1", name: null },
                    folder: null, subject: "Hi", from: { address: null, name: null },
                    to: [], date: null, snippet_html: "x", has_attachments: false,
                    score: 0.5, matched_arms: ["bm25"] }],
        next_cursor: null, total_estimate: null, took_ms: 42,
      });
    await search.submit();
    expect(search.snapshot.tookMs).toBe(42);
    (runSearch as unknown as { mockRejectedValueOnce: (v: unknown) => void })
      .mockRejectedValueOnce({ kind: "HttpError", detail: { status: 500 } });
    await search.submit();
    expect(search.snapshot.tookMs).toBe(null);
    expect(search.snapshot.results).toEqual([]);
    expect(search.snapshot.errorMessage).toContain("HttpError");
  });

  it("submit() stale response is discarded when a newer submit overtakes it", async () => {
    let resolveSlow!: () => void;
    const slowResp = {
      results: [{ message_id: "slow", account: { id: "1", name: null },
                  folder: null, subject: "slow", from: { address: null, name: null },
                  to: [], date: null, snippet_html: "slow", has_attachments: false,
                  score: 0.1, matched_arms: ["bm25"] }],
      next_cursor: null, total_estimate: null, took_ms: 999,
    };
    const fastResp = {
      results: [{ message_id: "fast", account: { id: "1", name: null },
                  folder: null, subject: "fast", from: { address: null, name: null },
                  to: [], date: null, snippet_html: "fast", has_attachments: false,
                  score: 0.9, matched_arms: ["bm25"] }],
      next_cursor: null, total_estimate: null, took_ms: 10,
    };
    (runSearch as unknown as { mockImplementationOnce: (f: () => Promise<unknown>) => void })
      .mockImplementationOnce(() => new Promise((r) => { resolveSlow = () => r(slowResp); }));
    (runSearch as unknown as { mockResolvedValueOnce: (v: unknown) => void })
      .mockResolvedValueOnce(fastResp);
    search.setQuery("first");
    const first = search.submit();
    search.setQuery("second");
    await search.submit();
    expect(search.snapshot.results[0]?.message_id).toBe("fast");
    expect(search.snapshot.tookMs).toBe(10);
    resolveSlow();
    await first;
    expect(search.snapshot.results[0]?.message_id).toBe("fast");
    expect(search.snapshot.tookMs).toBe(10);
    expect(search.snapshot.loading).toBe(false);
  });

  it("reset() clears everything", () => {
    search.setQuery("x");
    search.reset();
    expect(search.snapshot.query).toBe("");
    expect(search.snapshot.filters).toEqual(emptyFilters());
  });
});
