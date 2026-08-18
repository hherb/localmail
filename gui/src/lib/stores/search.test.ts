import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { search } from "./search.svelte";
import { emptyFilters } from "../api/search";
import { settings } from "./settings.svelte";

vi.mock("../tauri", () => ({
  runSearch: vi.fn(),
}));

import { runSearch } from "../tauri";

beforeEach(() => {
  settings.resetForTest();
});

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

  it("uses the configured page size and fallback language", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue({ results: [], next_cursor: null, total_estimate: null, took_ms: 1 });
    settings.setPageSize(25);
    settings.setDefaultLanguage("DE");
    search.setQuery("invoice");
    await search.submit();
    expect(runSearch).toHaveBeenCalledWith(expect.objectContaining({
      limit: 25,
      filters: expect.objectContaining({ lang: "de" }),
    }));
  });

  it("lets an explicit search language override the configured default", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue({ results: [], next_cursor: null, total_estimate: null, took_ms: 1 });
    settings.setDefaultLanguage("de");
    search.setFilters({ ...emptyFilters(), language: "fr" });
    await search.submit();
    expect(runSearch).toHaveBeenCalledWith(expect.objectContaining({
      filters: expect.objectContaining({ lang: "fr" }),
    }));
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

describe("search.loadMore", () => {
  beforeEach(() => {
    search.reset();
    vi.restoreAllMocks();
  });

  const row = (id: string) => ({
    message_id: id, account: { id: "1", name: null }, folder: null,
    subject: id, from: { address: null, name: null }, to: [], date: null,
    snippet_html: null, has_attachments: false, score: 1, matched_arms: [],
  });

  it("appends results and advances cursor", async () => {
    const tauri = await import("../tauri");
    const r1 = (id: string) => ({
      message_id: id, account: { id: "1", name: null }, folder: null,
      subject: id, from: { address: null, name: null }, to: [], date: null,
      snippet_html: null, has_attachments: false, score: 1, matched_arms: [],
    });
    vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [r1("1"), r1("2")], next_cursor: "tok:2",
        total_estimate: null, took_ms: 1,
      })
      .mockResolvedValueOnce({
        results: [r1("3")], next_cursor: null,
        total_estimate: null, took_ms: 1,
      });
    search.setQuery("hello");
    await search.submit();
    await search.loadMore();
    expect(search.snapshot.results.map((r) => r.message_id))
      .toEqual(["1", "2", "3"]);
  });

  it("is a no-op when hasMore is false", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch").mockResolvedValue({
      results: [], next_cursor: null, total_estimate: null, took_ms: 1,
    });
    search.setQuery("hello");
    await search.submit();
    spy.mockClear();
    await search.loadMore();
    expect(spy).not.toHaveBeenCalled();
  });

  it("on 409 cursor-expired, re-submits and drops prior count", async () => {
    const tauri = await import("../tauri");
    const r1 = (id: string) => ({
      message_id: id, account: { id: "1", name: null }, folder: null,
      subject: id, from: { address: null, name: null }, to: [], date: null,
      snippet_html: null, has_attachments: false, score: 1, matched_arms: [],
    });
    vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [r1("1"), r1("2")], next_cursor: "tok:2",
        total_estimate: null, took_ms: 1,
      })
      .mockRejectedValueOnce(new Error(
        '{"type":"/problems/search-cursor-expired","detail":"gone"}',
      ))
      .mockResolvedValueOnce({
        results: [r1("1"), r1("2"), r1("3"), r1("4")], next_cursor: "tok2:2",
        total_estimate: null, took_ms: 1,
      });
    search.setQuery("hello");
    await search.submit();
    await search.loadMore();
    expect(search.snapshot.results.map((r) => r.message_id))
      .toEqual(["1", "2", "3", "4"]);
  });

  it("omits sort when paging, so the cursor's ordering cannot be contradicted", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [row("1")], next_cursor: "tok:2",
        total_estimate: null, took_ms: 1,
      })
      .mockResolvedValueOnce({
        results: [row("2")], next_cursor: null,
        total_estimate: null, took_ms: 1,
      });
    search.setQuery("hello");
    search.setSort("date");
    await search.submit();
    await search.loadMore();
    expect(spy.mock.calls[0][0].sort).toBe("date");
    expect(spy.mock.calls[1][0].cursor).toBe("tok:2");
    expect(spy.mock.calls[1][0].sort).toBeUndefined();
  });

  it("does not page while a fresh search is in flight", async () => {
    // Without this guard the paging response appends the *old* query's rows to
    // the *new* query's results — both requests share #submitSeq, so neither
    // discards the other.
    const tauri = await import("../tauri");
    let release: ((v: unknown) => void) | undefined;
    const spy = vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [row("1")], next_cursor: "tok:2",
        total_estimate: null, took_ms: 1,
      })
      .mockImplementationOnce(
        () => new Promise((resolve) => { release = resolve as (v: unknown) => void; }),
      );
    search.setQuery("hello");
    await search.submit();
    const inFlight = search.submit();
    spy.mockClear();

    await search.loadMore();

    expect(spy).not.toHaveBeenCalled();
    release?.({ results: [], next_cursor: null, total_estimate: null, took_ms: 1 });
    await inFlight;
  });

  it("on 400 stops paging instead of re-issuing a request that cannot succeed", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [row("1"), row("2")], next_cursor: "tok:2",
        total_estimate: null, took_ms: 1,
      })
      .mockRejectedValueOnce({
        kind: "Http",
        detail: {
          kind: "HttpStatus",
          detail: { status: 400, body: '{"type":"/problems/validation-failed"}' },
        },
      });
    search.setQuery("hello");
    await search.submit();

    await search.loadMore();

    expect(search.snapshot.hasMore).toBe(false);
    expect(search.snapshot.cursor).toBe(null);
    expect(search.snapshot.errorMessage).not.toBe(null);
    // The rows already fetched still answer the user's query — only the
    // continuation is dead, so they stay on screen.
    expect(search.snapshot.results.map((r) => r.message_id)).toEqual(["1", "2"]);

    spy.mockClear();
    await search.loadMore();
    expect(spy).not.toHaveBeenCalled();
  });

  it("on 409 the recovery re-run states the sort, having no cursor to inherit it from", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [row("1")], next_cursor: "K|abc",
        total_estimate: null, took_ms: 1,
      })
      .mockRejectedValueOnce(new Error(
        '{"type":"/problems/search-cursor-expired"}',
      ))
      .mockResolvedValueOnce({
        results: [row("1"), row("2")], next_cursor: null,
        total_estimate: null, took_ms: 1,
      });
    search.setQuery("hello");
    search.setSort("date");
    await search.submit();
    await search.loadMore();
    const recovery = spy.mock.calls[2][0];
    expect(recovery.cursor).toBe(null);
    expect(recovery.sort).toBe("date");
  });

  it("on 409 when re-submitted pool is smaller, falls back to full reset", async () => {
    const tauri = await import("../tauri");
    const r1 = (id: string) => ({
      message_id: id, account: { id: "1", name: null }, folder: null,
      subject: id, from: { address: null, name: null }, to: [], date: null,
      snippet_html: null, has_attachments: false, score: 1, matched_arms: [],
    });
    vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [r1("1"), r1("2"), r1("3")], next_cursor: "tok:2",
        total_estimate: null, took_ms: 1,
      })
      .mockRejectedValueOnce(new Error(
        '{"type":"/problems/search-cursor-expired"}',
      ))
      .mockResolvedValueOnce({
        results: [r1("9")], next_cursor: null,
        total_estimate: null, took_ms: 1,
      });
    search.setQuery("hello");
    await search.submit();
    await search.loadMore();
    expect(search.snapshot.results.map((r) => r.message_id)).toEqual(["9"]);
  });
});
