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

/**
 * #324 — the sort the store puts on the wire.
 *
 * `statedSort` itself is unit-tested in `../search_paging.test.ts`; these pin
 * what the *store* sends, which a rule test cannot see. The case that matters
 * is the one the server refuses: a query it cannot rank. It decides that after
 * lifting filter operators out, so `from:alice` — which `SearchBar`'s own
 * placeholder advertises — is textless server-side even though the box is not
 * empty. Stating the store's default `rank` for it would turn an advertised
 * search into an error banner, so `rank` is never stated at all.
 */
describe("search store: the sort it states (#324)", () => {
  beforeEach(() => {
    search.reset();
    vi.restoreAllMocks();
  });

  const empty = {
    results: [], next_cursor: null, total_estimate: null, took_ms: 1,
  };

  it("omits the default rank sort when the box is empty but a filter is set", async () => {
    // "Everything from this account" — a shipped flow the refusal must not
    // break. The server resolves an unstated sort to the date walk, which is
    // exactly what this flow received before #324.
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch").mockResolvedValue(empty);
    search.setFilters({ ...emptyFilters(), from: "anna" });
    await search.submit();
    expect(spy.mock.calls[0][0].sort).toBeUndefined();
  });

  it("omits rank for a box holding only filter operators, which the server cannot rank", async () => {
    // The regression a `query.trim() !== ""` rule would reintroduce: this is
    // textless server-side, so stating `rank` is a 400 and the store would
    // clear its results and show a banner for a search the placeholder
    // advertises.
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch").mockResolvedValue(empty);
    search.setQuery("from:alice");
    await search.submit();
    expect(spy.mock.calls[0][0].sort).toBeUndefined();
  });

  it("omits rank for an ordinary text query too, where it is merely redundant", async () => {
    // Equivalent, not lossy: an unstated sort resolves server-side to `rank`
    // whenever there is text to rank.
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch").mockResolvedValue(empty);
    search.setQuery("invoice");
    await search.submit();
    expect(spy.mock.calls[0][0].sort).toBeUndefined();
  });

  it("still states date, so the sort selector is not inert", async () => {
    // The positive control: a store that dropped every sort would pass every
    // assertion above while silently retiring the Date option.
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch").mockResolvedValue(empty);
    search.setSort("date");
    search.setQuery("invoice");
    await search.submit();
    expect(spy.mock.calls[0][0].sort).toBe("date");
  });

  it("omits it on the 409 recovery re-run too, which is a fresh request", async () => {
    // The recovery has no cursor to inherit an ordering from, so it is a
    // second place a sort could reach the wire — and it fires exactly when the
    // user is already looking at a failure.
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [], next_cursor: "K|abc", total_estimate: null, took_ms: 1,
      })
      .mockRejectedValueOnce(new Error(
        '{"type":"/problems/search-cursor-expired"}',
      ))
      .mockResolvedValueOnce(empty);
    search.setFilters({ ...emptyFilters(), from: "anna" });
    await search.submit();
    await search.loadMore();
    expect(spy.mock.calls[2][0].cursor).toBe(null);
    expect(spy.mock.calls[2][0].sort).toBeUndefined();
  });

  it("still states date on the 409 recovery, which is what keeps it a date search", async () => {
    // Mutation-relevant: a recovery that dropped the sort would silently flip
    // a `sort=date` search back to the server's default ordering.
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [], next_cursor: "K|abc", total_estimate: null, took_ms: 1,
      })
      .mockRejectedValueOnce(new Error(
        '{"type":"/problems/search-cursor-expired"}',
      ))
      .mockResolvedValueOnce(empty);
    search.setSort("date");
    search.setQuery("invoice");
    await search.submit();
    await search.loadMore();
    expect(spy.mock.calls[2][0].sort).toBe("date");
  });
});

/**
 * #353 — whether ranking was possible at all, which the ordering cannot say.
 *
 * `sort_applied` is `date` both for a query the server could not rank and
 * for a text query whose caller asked for date. The selector inferred the
 * first from the second and re-enabled Relevance on unrankable queries once
 * a click could change the request. The store records `rankable` off every
 * response, at all four sites an ordering reaches it.
 */
describe("search store: whether the query could be ranked (#353)", () => {
  const page = (
    rankable: boolean | undefined,
    cursor: string | null = null,
  ) => ({
    results: [], next_cursor: cursor, total_estimate: null, took_ms: 1,
    sort_applied: "date",
    ...(rankable === undefined ? {} : { rankable }),
  });

  it("records an unrankable query from a fresh search", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue(page(false));
    search.setQuery("from:alice");
    await search.submit();
    expect(search.snapshot.rankable).toBe(false);
  });

  it("records a rankable query whose caller chose date ordering", async () => {
    // The discriminating pair: `sort_applied` is "date" in both these tests
    // and only `rankable` separates them.
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue(page(true));
    search.setQuery("invoice");
    await search.submit();
    expect(search.snapshot.sortApplied).toBe("date");
    expect(search.snapshot.rankable).toBe(true);
  });

  it("reads null from a server that does not send the key", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue(page(undefined));
    search.setQuery("invoice");
    await search.submit();
    expect(search.snapshot.rankable).toBe(null);
  });

  it("clears it when a search fails", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue(page(false));
    search.setQuery("from:alice");
    await search.submit();
    expect(search.snapshot.rankable).toBe(false);
    (runSearch as unknown as { mockRejectedValue: (v: unknown) => void })
      .mockRejectedValue(new Error("boom"));
    await search.submit();
    // Nothing on screen for it to describe, so it claims nothing.
    expect(search.snapshot.rankable).toBe(null);
  });

  it("a continuation page records it too", async () => {
    // Page 1 omits the key, so the only way to reach `false` is for
    // `loadMore` to read the continuation's value — the vacuity trap the
    // #345 sibling above documents.
    const m = runSearch as unknown as {
      mockResolvedValueOnce: (v: unknown) => void;
    };
    m.mockResolvedValueOnce(page(undefined, "K|abc"));
    search.setQuery("from:alice");
    await search.submit();
    expect(search.snapshot.rankable).toBe(null);
    m.mockResolvedValueOnce(page(false, null));
    await search.loadMore();
    expect(search.snapshot.rankable).toBe(false);
  });

  it("the 409 recovery records it as well", async () => {
    const m = runSearch as unknown as {
      mockResolvedValueOnce: (v: unknown) => void;
      mockRejectedValueOnce: (v: unknown) => void;
    };
    m.mockResolvedValueOnce(page(undefined, "tok:2"));
    search.setQuery("from:alice");
    await search.submit();
    expect(search.snapshot.rankable).toBe(null);
    m.mockRejectedValueOnce(new Error(
      '{"type":"/problems/search-cursor-expired","detail":"gone"}',
    ));
    m.mockResolvedValueOnce(page(false, null));
    await search.loadMore();
    expect(search.snapshot.rankable).toBe(false);
  });
});

/**
 * #345 — the ordering the store *displays* is the one the server ran.
 *
 * `sort` is a request and the server resolves it from the query, so the two
 * differ for a textless search. The store therefore records `sort_applied`
 * off every response; `sort_display.ts` unit-tests what the selector then
 * shows. These pin that the field reaches the store at all, and that it keeps
 * describing the rows currently on screen.
 */
describe("search store: the ordering that ran (#345)", () => {
  const page = (sortApplied: string | undefined, cursor: string | null = null) => ({
    results: [], next_cursor: cursor, total_estimate: null, took_ms: 1,
    ...(sortApplied === undefined ? {} : { sort_applied: sortApplied }),
  });

  it("records the applied sort from a fresh search", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue(page("date"));
    search.setQuery("from:alice");
    await search.submit();
    expect(search.snapshot.sortApplied).toBe("date");
  });

  it("records rank when the server ranked", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue(page("rank"));
    search.setQuery("invoice");
    await search.submit();
    expect(search.snapshot.sortApplied).toBe("rank");
  });

  it("reads null from a server that does not send the key", async () => {
    // Graceful degradation, the `is_admin` precedent: an older `serve` leaves
    // the field absent and the selector falls back to showing the request,
    // which is exactly the pre-#345 behaviour rather than a wrong claim.
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue(page(undefined));
    search.setQuery("invoice");
    await search.submit();
    expect(search.snapshot.sortApplied).toBe(null);
  });

  it("clears the applied sort when a search fails", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue(page("date"));
    search.setQuery("from:alice");
    await search.submit();
    expect(search.snapshot.sortApplied).toBe("date");
    (runSearch as unknown as { mockRejectedValue: (v: unknown) => void })
      .mockRejectedValue(new Error("boom"));
    await search.submit();
    // The rows are gone, so nothing is on screen for it to describe.
    expect(search.snapshot.sortApplied).toBe(null);
  });

  it("a continuation page keeps the field describing the rows on screen", async () => {
    // The two pages must NOT agree, or `submit()`'s own write satisfies the
    // assertion and the continuation's write is untested — this test was
    // vacuous while both were "date". Page 1 omits the key so the only way
    // to reach "date" is for `loadMore` to read the continuation's value.
    const m = runSearch as unknown as {
      mockResolvedValueOnce: (v: unknown) => void;
    };
    m.mockResolvedValueOnce(page(undefined, "K|abc"));
    search.setQuery("from:alice");
    await search.submit();
    expect(search.snapshot.sortApplied).toBe(null);
    m.mockResolvedValueOnce(page("date", null));
    await search.loadMore();
    expect(search.snapshot.sortApplied).toBe("date");
  });

  it("the 409 recovery records the fresh search's ordering", async () => {
    // The recovery re-runs *without* a cursor, so it is the second place an
    // ordering reaches the store — and the only continuation where a
    // re-resolution can legitimately change the answer. `search_paging.ts`'s
    // sort rule is pinned on both halves of this path; this field was not.
    const m = runSearch as unknown as {
      mockResolvedValueOnce: (v: unknown) => void;
      mockRejectedValueOnce: (v: unknown) => void;
    };
    m.mockResolvedValueOnce(page(undefined, "tok:2"));
    search.setQuery("from:alice");
    await search.submit();
    expect(search.snapshot.sortApplied).toBe(null);
    m.mockRejectedValueOnce(new Error(
      '{"type":"/problems/search-cursor-expired","detail":"gone"}',
    ));
    m.mockResolvedValueOnce(page("date", null));
    await search.loadMore();
    expect(search.snapshot.sortApplied).toBe("date");
  });

  it("a 400 that retires the cursor leaves the field alone", async () => {
    // The rows already fetched stay on screen, so the ordering that
    // produced them is still what the selector should describe.
    const m = runSearch as unknown as {
      mockResolvedValueOnce: (v: unknown) => void;
      mockRejectedValueOnce: (v: unknown) => void;
    };
    m.mockResolvedValueOnce(page("date", "K|abc"));
    search.setQuery("from:alice");
    await search.submit();
    // The nested {kind, detail} shape `httpStatusOf` walks — the same
    // fixture `search_paging.test.ts` uses for this status.
    m.mockRejectedValueOnce({
      kind: "Http",
      detail: { kind: "HttpStatus", detail: { status: 400, body: "{}" } },
    });
    await search.loadMore();
    expect(search.snapshot.hasMore).toBe(false);
    expect(search.snapshot.sortApplied).toBe("date");
  });

  it("reset() forgets it", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue(page("date"));
    search.setQuery("from:alice");
    await search.submit();
    search.reset();
    expect(search.snapshot.sortApplied).toBe(null);
  });
});
