"""Search engine orchestrator + pure helpers (RRF, snippets).

Most of this module is the Searcher class (Tasks 14–19); this commit
introduces only the data shapes and rrf_fuse so later tasks can build on
top.
"""

from __future__ import annotations

import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import MINYEAR, datetime, timezone
from typing import Any, Literal

SortMode = Literal["rank", "date"]

# Sentinel for "no usable date" — sorts strictly older than any real
# timestamp so NULLs land at the end of a `sort=date` page under
# Python's reverse=True ordering.
_DATE_SORT_NULL_SENTINEL = datetime(MINYEAR, 1, 1, tzinfo=timezone.utc)


def _date_sort_key(item: dict) -> tuple[int, datetime]:
    """Key for ``COALESCE(internal_date, date_sent) DESC NULLS LAST``.

    Returned tuple uses (1, dt) for rows with a usable date and (0,
    sentinel) for NULLs, so Python's default ascending sort puts NULLs
    first; ``sorted(..., reverse=True)`` then reverses to (newest, ...,
    older, NULLs-last) — matching the canonical ordering in the SQL
    paths.
    """
    msg = item.get("msg") or {}
    dt = msg.get("internal_date") or msg.get("date_sent")
    if dt is None:
        return (0, _DATE_SORT_NULL_SENTINEL)
    return (1, dt)

import psycopg
from psycopg_pool import ConnectionPool

from localmail.config import SearchConfig
from localmail.search.embeddings import EmbeddingBackend
from localmail.search.page_cache import (
    CacheMissError, PageCache, PageOutOfPoolError,
)
from localmail.search.query import ParsedQuery, parse_query
from localmail.search.reranker import Reranker

log = logging.getLogger("localmail.search.searcher")


def _safe_rerank(
    reranker: Reranker,
    query: str,
    snippets: list[str],
    *,
    fallback: list[float],
) -> list[float]:
    """Call the reranker, but degrade to ``fallback`` if it raises.

    A broken reranker (wrong output shape, missing model file, OOM) should
    not turn a search into a 500 — the RRF-fused scores from the retrieval
    arms are usable on their own. The mismatch is logged as a WARNING so
    ops sees the degraded quality.
    """
    try:
        return reranker.rerank(query, snippets)
    except Exception as exc:
        log.warning(
            "reranker %r raised %s: %s — falling back to fused RRF scores",
            getattr(reranker, "model", type(reranker).__name__),
            type(exc).__name__,
            exc,
        )
        return fallback


@dataclass(frozen=True)
class ArmHit:
    """One hit from one retrieval arm."""
    message_id: int
    chunk_id: int | None  # None for Arm 1 (whole-message BM25)
    chunk_table: Literal["message", "message_chunks", "attachment_chunks"]
    arm_score: float
    rank: int  # 1-based, within the arm


@dataclass(frozen=True)
class FusedHit:
    """Post-RRF hit, deduplicated to one row per message_id."""
    message_id: int
    best_chunk_id: int | None
    best_chunk_table: Literal["message", "message_chunks", "attachment_chunks"]
    rrf_score: float
    contributing_arms: list[int] = field(default_factory=list)


def rrf_fuse(arms: list[list[ArmHit]], k: int) -> list[FusedHit]:
    """Reciprocal Rank Fusion across N arms.

    Contribution of arm i to (message_id, chunk_id) is 1 / (k + rank).
    Output is one FusedHit per message_id, keeping the chunk whose own
    single-arm contribution is largest (so the snippet later comes from
    the chunk that 'earned' the rank). Sorted by descending rrf_score.

    Note on the winner-chunk pick: Arm 1 (whole-message BM25) contributes
    `(message_id, chunk_id=None)`; Arms 2/3 contribute `(message_id, chunk_id=X)`.
    When Arm 1 dominates the score for a message, `best_chunk_id` will be
    None and the snippet path falls back to `messages.body_text`. That's
    fine for header-driven hits but means a chunk-level match might be
    displayed with the leading body window rather than its own chunk text;
    re-ranking still considers all hydrated candidates.

    `k` is the standard RRF dampening constant (default 60).
    """
    # Per-message aggregated score + per-chunk contributions (for winner pick)
    agg: dict[int, dict] = {}
    for arm_idx, arm in enumerate(arms):
        for hit in arm:
            entry = agg.setdefault(hit.message_id, {
                "score": 0.0,
                "arms": set(),
                "chunks": {},  # (chunk_id, chunk_table) -> best contribution
            })
            contrib = 1.0 / (k + hit.rank)
            entry["score"] += contrib
            entry["arms"].add(arm_idx)
            chkey = (hit.chunk_id, hit.chunk_table)
            if contrib > entry["chunks"].get(chkey, 0.0):
                entry["chunks"][chkey] = contrib

    out: list[FusedHit] = []
    for mid, entry in agg.items():
        (best_cid, best_table), _ = max(entry["chunks"].items(), key=lambda kv: kv[1])
        out.append(FusedHit(
            message_id=mid,
            best_chunk_id=best_cid,
            best_chunk_table=best_table,
            rrf_score=entry["score"],
            contributing_arms=sorted(entry["arms"]),
        ))
    out.sort(key=lambda h: h.rrf_score, reverse=True)
    return out


_WORD = re.compile(r"\w+", re.UNICODE)


def make_snippet(chunk_text: str, query_terms: list[str], width: int) -> str:
    """Return a ~`width`-char window around the strongest query-term match.

    - If chunk is shorter than width, returned in full.
    - If no query term matches, returns the leading window.
    - Match is case-insensitive, word-boundary-aware.
    """
    if not chunk_text:
        return ""
    if len(chunk_text) <= width:
        return chunk_text

    best_pos: int | None = None
    lowered = chunk_text.lower()
    for term in query_terms:
        if not term:
            continue
        idx = lowered.find(term.lower())
        if idx != -1 and (best_pos is None or idx < best_pos):
            best_pos = idx
    if best_pos is None:
        # Leading window, snapped to word boundary
        cut = chunk_text[:width]
        m = list(_WORD.finditer(cut))
        if m and m[-1].end() < len(cut):
            cut = cut[: m[-1].end()]
        return cut

    half = width // 2
    start = max(0, best_pos - half)
    end = min(len(chunk_text), start + width)
    snippet = chunk_text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(chunk_text) else ""
    return f"{prefix}{snippet}{suffix}".strip()


@dataclass(frozen=True)
class SearchResult:
    """One ranked search hit, with the snippet that earned the rank."""
    message_id: int
    account_id: int
    rank: int
    score: float
    rrf_score: float
    subject: str | None
    from_addr: str | None
    from_name: str | None
    date_sent: datetime | None
    snippet: str
    snippet_source: Literal["header", "body", "attachment"]
    attachment_filename: str | None
    matched_chunk_id: int | None
    matched_chunk_table: Literal["message", "message_chunks", "attachment_chunks"]


@dataclass(frozen=True)
class SearchPage:
    """One page of results plus pagination metadata."""
    results: list[SearchResult]
    page: int
    page_size: int
    pool_size: int
    candidates_per_arm: int
    has_more_in_pool: bool
    can_grow_pool: bool
    search_token: str | None
    query: ParsedQuery
    timing_ms: dict[str, float]


class Searcher:
    """Orchestrates the hybrid search pipeline.

    Created once per process and reused — holds long-lived backend handles
    and the page cache. Methods:
      - search(query, ...) -> SearchPage  (the entry point)
      - continue_page(token, page) -> SearchPage  (Task 16)
      - grow_pool(token, candidates_per_arm) -> SearchPage  (Task 16)
    """

    def __init__(
        self,
        pool: ConnectionPool,
        cfg: SearchConfig,
        embeddings: EmbeddingBackend,
        reranker: Reranker | None,
        rewriter: Any | None = None,  # QueryRewriter type lands Phase 4
    ) -> None:
        self._pool = pool
        self._cfg = cfg
        self._embeddings = embeddings
        self._reranker = reranker
        self._rewriter = rewriter
        self._cache = PageCache(maxsize=cfg.page_cache_size, ttl_s=cfg.page_cache_ttl_s)
        self._lang_warning_emitted = False

    def _maybe_warn_unpopulated_body_lang(
        self, conn: psycopg.Connection, parsed: ParsedQuery,
    ) -> None:
        """One-shot warning when `lang:` filters target an empty body_lang column.

        Migration 0015 adds `messages.body_lang`; the embed worker populates
        it lazily and `localmail lang-backfill` drains the existing archive.
        A user running `lang:de` against an unpopulated column gets zero
        results with no hint as to why; this nudge tells them exactly that.
        The probe uses the partial index so cost is O(1).
        """
        if self._lang_warning_emitted:
            return
        if not parsed.filters.languages:
            return
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM messages WHERE body_lang IS NOT NULL LIMIT 1")
            if cur.fetchone() is None:
                log.warning(
                    "search: `lang:` filter present but messages.body_lang is "
                    "not populated for any row; query will return 0 hits. "
                    "Run `localmail lang-backfill` to populate the column."
                )
        self._lang_warning_emitted = True

    def _resolve_account_names(self, conn: psycopg.Connection, parsed: ParsedQuery) -> ParsedQuery:
        if not parsed.filters.account_names:
            return parsed
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, id FROM accounts WHERE name = ANY(%s)",
                (parsed.filters.account_names,),
            )
            found: dict[str, int] = dict(cur.fetchall())
        unknown = [n for n in parsed.filters.account_names if n not in found]
        if unknown:
            log.warning(
                "search: account name(s) %s do not exist; matching no rows for that filter",
                unknown,
            )
        ids = list(found.values())
        from dataclasses import replace
        return replace(parsed, filters=replace(parsed.filters, accounts=ids))

    def _list_recent_messages(
        self,
        conn: psycopg.Connection,
        parsed: ParsedQuery,
        limit: int,
    ) -> list["SearchResult"]:
        """Empty-query fallback: SELECT messages ORDER BY
        ``COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC``.

        ``internal_date`` (migration 0018) holds the IMAP server's
        INTERNALDATE — when the email actually arrived at the mailbox.
        sync.py populates it on insert; legacy rows are populated via
        ``localmail backfill-internal-date``. ``date_sent`` (header
        ``Date:``) is the fallback for rows not yet backfilled. The
        expression matches the ``messages_recent_idx`` index so the
        planner can avoid a full table sort.

        Shares ``_filter_sql`` with the retrieval arms so structured
        filters (account_id, folder_id, from/to/subject substrings, date
        ranges, has_attachment, lang) behave identically here and in the
        full-pipeline path. Returns ``SearchResult`` so the API layer can
        marshal results uniformly regardless of which branch fired.
        """
        from localmail.search.arms import _filter_sql
        where_extra, where_params = _filter_sql(parsed.filters)
        sql = f"""
            SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name, m.date_sent
              FROM messages m
             WHERE TRUE
             {where_extra}
             ORDER BY COALESCE(m.internal_date, m.date_sent) DESC NULLS LAST, m.id DESC
             LIMIT %s
        """
        params: list[Any] = [*where_params, limit]
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        out: list[SearchResult] = []
        for rank, (mid, account_id, subject, from_addr, from_name, date_sent) in enumerate(rows, start=1):
            out.append(SearchResult(
                message_id=mid,
                account_id=account_id,
                rank=rank,
                score=1.0 / rank,
                rrf_score=0.0,
                subject=subject,
                from_addr=from_addr,
                from_name=from_name,
                date_sent=date_sent,
                snippet="",
                snippet_source="header",
                attachment_filename=None,
                matched_chunk_id=None,
                matched_chunk_table="message",
            ))
        return out

    def _retrieve_pool(
        self,
        conn: psycopg.Connection,
        parsed: ParsedQuery,
        candidates_per_arm: int,
        rerank_pool_size: int,
    ) -> list[FusedHit]:
        # Lazy import to avoid circular dependency (arms imports ArmHit from this module)
        from localmail.search.arms import (
            arm_bm25_chunks, arm_bm25_messages, arm_vector_chunks,
            arm_vector_attachment_chunks,
        )
        a1 = arm_bm25_messages(conn, parsed, self._cfg, limit=candidates_per_arm)
        a2 = arm_bm25_chunks(conn, parsed, self._cfg, limit=candidates_per_arm)
        qvec = self._embeddings.embed_query(parsed.rewritten_text or parsed.free_text)
        a3 = arm_vector_chunks(conn, parsed, self._cfg, qvec, limit=candidates_per_arm)
        a4 = arm_vector_attachment_chunks(conn, parsed, self._cfg, qvec, limit=candidates_per_arm)
        fused = rrf_fuse([a1, a2, a3, a4], k=self._cfg.rrf_k)
        return fused[:rerank_pool_size]

    def _hydrate(self, conn: psycopg.Connection, fused: list[FusedHit]) -> list[dict]:
        """Pull message + chunk text for each fused hit, returned in fused order.

        For message_chunks hits, also fetches the chunk text and kind so that
        the snippet reflects the exact matched chunk rather than the message body.
        For attachment_chunks hits, fetches chunk text and the sha256 (bytes) so
        that _build_results can later resolve attachment_filename from the carrying
        message's JSONB attachments column.
        """
        if not fused:
            return []
        msg_ids = [h.message_id for h in fused]
        msgs: dict[int, dict] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, subject, from_addr, from_name, date_sent,"
                " internal_date, body_text FROM messages WHERE id = ANY(%s)", (msg_ids,))
            for mid, acct, subj, fa, fn, ds, intd, body in cur.fetchall():
                msgs[mid] = {"account_id": acct, "subject": subj, "from_addr": fa,
                             "from_name": fn, "date_sent": ds, "internal_date": intd,
                             "body_text": body}

            # Fetch message_chunks text+kind for Arms 2 and 3 hits.
            msg_chunk_ids = [
                h.best_chunk_id for h in fused
                if h.best_chunk_id and h.best_chunk_table == "message_chunks"
            ]
            msg_chunks: dict[int, tuple[str, str]] = {}
            if msg_chunk_ids:
                cur.execute(
                    "SELECT id, text, kind FROM message_chunks WHERE id = ANY(%s)",
                    (msg_chunk_ids,),
                )
                msg_chunks = {cid: (t, k) for cid, t, k in cur.fetchall()}

            # Fetch attachment_chunks text+sha256 for Arm 4 hits.
            att_chunk_ids = [
                h.best_chunk_id for h in fused
                if h.best_chunk_id and h.best_chunk_table == "attachment_chunks"
            ]
            att_chunks: dict[int, tuple[str, bytes]] = {}
            if att_chunk_ids:
                cur.execute(
                    "SELECT id, text, sha256 FROM attachment_chunks WHERE id = ANY(%s)",
                    (att_chunk_ids,),
                )
                att_chunks = {cid: (t, sha) for cid, t, sha in cur.fetchall()}

        out = []
        for h in fused:
            m = msgs.get(h.message_id, {})
            chunk_kind: str | None = None
            attachment_sha256_hex: str | None = None

            if h.best_chunk_table == "attachment_chunks" and h.best_chunk_id in att_chunks:
                snip_text, sha_bytes = att_chunks[h.best_chunk_id]
                attachment_sha256_hex = sha_bytes.hex()
            elif (h.best_chunk_table != "attachment_chunks"
                  and h.best_chunk_id and h.best_chunk_id in msg_chunks):
                snip_text, chunk_kind = msg_chunks[h.best_chunk_id]
            else:
                snip_text = m.get("body_text") or ""

            out.append({
                "fused": h,
                "msg": m,
                "snippet_source_text": snip_text or "",
                "chunk_kind": chunk_kind,
                "attachment_sha256_hex": attachment_sha256_hex,
            })
        return out

    def _build_results(
        self,
        hydrated: list[dict],
        parsed: ParsedQuery,
        rerank_scores: list[float],
        page: int,
        page_size: int,
        conn: psycopg.Connection | None = None,
        sort: SortMode = "rank",
    ) -> list[SearchResult]:
        """Assemble SearchResult objects for one page from the ordered hydrated pool.

        `conn` is required when any hydrated item has snippet_source='attachment' so
        that attachment_filename can be resolved via a JSONB lookup. It is optional
        (and unused) when no attachment hits are present, preserving backward
        compatibility with callers that hold no live connection at this point.

        `sort="rank"` (default) orders by rerank score — the relevance-first
        behavior. `sort="date"` keeps the same retrieval pool (so "what
        matches" is unchanged) but orders the page by
        ``COALESCE(internal_date, date_sent) DESC NULLS LAST`` so callers
        that want "relevant emails, newest first" don't have to bolt on a
        separate date-list endpoint.
        """
        terms = parsed.free_text.split()
        if sort == "date":
            ordered = sorted(
                zip(hydrated, rerank_scores, strict=True),
                key=lambda x: _date_sort_key(x[0]), reverse=True,
            )
        else:
            ordered = sorted(
                zip(hydrated, rerank_scores, strict=True),
                key=lambda x: x[1], reverse=True,
            )
        start = (page - 1) * page_size
        end = start + page_size
        out: list[SearchResult] = []
        for i, (item, score) in enumerate(ordered[start:end], start=1):
            h = item["fused"]
            m = item["msg"]
            snip = make_snippet(
                item["snippet_source_text"], terms,
                width=self._cfg.snippet_width_chars,
            )
            if h.best_chunk_table == "attachment_chunks":
                source: Literal["header", "body", "attachment"] = "attachment"
            elif item.get("chunk_kind") == "body":
                source = "body"
            else:
                source = "header"

            attachment_filename: str | None = None
            if source == "attachment":
                sha_hex = item.get("attachment_sha256_hex")
                if sha_hex and conn is not None:
                    # Resolve the original filename from the carrying message's
                    # JSONB attachments column. The N+1 lookup is bounded by
                    # page_size, so it's acceptable here.
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT elem ->> 'filename' "
                            "FROM messages, jsonb_array_elements(attachments) elem "
                            "WHERE messages.id = %s AND elem ->> 'sha256' = %s "
                            "LIMIT 1",
                            (h.message_id, sha_hex),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            attachment_filename = row[0]

            out.append(SearchResult(
                message_id=h.message_id, account_id=m.get("account_id", 0),
                rank=i, score=float(score), rrf_score=h.rrf_score,
                subject=m.get("subject"), from_addr=m.get("from_addr"),
                from_name=m.get("from_name"), date_sent=m.get("date_sent"),
                snippet=snip, snippet_source=source,
                attachment_filename=attachment_filename,
                matched_chunk_id=h.best_chunk_id,
                matched_chunk_table=h.best_chunk_table,
            ))
        return out

    def continue_page(
        self, search_token: str, page: int, *, user_id: int | None = None,
    ) -> SearchPage:
        """Serve subsequent pages from the cached pool. Raises if past pool's end.

        ``user_id`` namespaces cached pools so a cursor minted by user A cannot
        be replayed by user B. If the cached pool's ``user_id`` does not
        match, the cache lookup is treated as a miss — the caller can fall
        back to a fresh search rather than reusing another user's pool.

        Zero DB round-trips when the page slice contains no attachment hits.
        Opens a short-lived connection only when filename resolution is needed
        (i.e. at least one hit in the hydrated pool has best_chunk_table ==
        'attachment_chunks').
        """
        import math
        entry = self._cache.get(search_token)  # may raise CacheMissError
        if user_id is not None and entry.get("user_id") != user_id:
            raise CacheMissError(search_token)
        page_size = entry["page_size"]
        hydrated = entry["hydrated"]
        pool_size = len(hydrated)
        max_page = max(1, math.ceil(pool_size / page_size))
        if page < 1 or page > max_page:
            raise PageOutOfPoolError(
                f"page {page} out of pool (pool_size={pool_size}, page_size={page_size}); "
                "call grow_pool to widen the candidate pool"
            )
        needs_filename = any(
            item["fused"].best_chunk_table == "attachment_chunks" for item in hydrated
        )
        sort: SortMode = entry.get("sort", "rank")
        if needs_filename:
            with self._pool.connection() as conn:
                results = self._build_results(
                    hydrated, entry["parsed"], entry["scores"], page, page_size,
                    conn=conn, sort=sort,
                )
        else:
            results = self._build_results(
                hydrated, entry["parsed"], entry["scores"], page, page_size,
                conn=None, sort=sort,
            )
        return SearchPage(
            results=results, page=page, page_size=page_size, pool_size=pool_size,
            candidates_per_arm=entry["candidates_per_arm"],
            has_more_in_pool=pool_size > page * page_size,
            can_grow_pool=True,
            search_token=search_token, query=entry["parsed"],
            timing_ms={"cache_hit": 0.0},
        )

    def grow_pool(
        self, search_token: str, candidates_per_arm: int, *, user_id: int | None = None,
    ) -> SearchPage:
        """Re-run the pipeline with a larger candidate pool. Returns page 1.

        ``user_id`` enforces the same cache-scoping invariant as
        :meth:`continue_page`: a cursor minted by user A is treated as
        unknown when presented under user B's identity.
        """
        entry = self._cache.get(search_token)
        if user_id is not None and entry.get("user_id") != user_id:
            raise CacheMissError(search_token)
        parsed = entry["parsed"]
        sort: SortMode = entry.get("sort", "rank")
        self._cache.invalidate(search_token)
        # rerank pool grows proportionally so the larger arm output isn't wasted
        rps = max(candidates_per_arm, entry["rerank_pool_size"])
        page = self._search_with_parsed(parsed, page_size=entry["page_size"],
                                        candidates_per_arm=candidates_per_arm,
                                        rerank_pool_size=rps, use_cache=True,
                                        user_id=user_id, sort=sort)
        return page

    def _search_with_parsed(self, parsed, *, page_size, candidates_per_arm,
                            rerank_pool_size, use_cache, user_id: int | None = None,
                            sort: SortMode = "rank"):
        """Variant of search() that takes an already-parsed query.

        Connection scope: retrieval + hydrate inside one 'with' block. The
        connection is released before the reranker runs (the ML pass can be
        slow). A second short-lived connection opens only when the page needs
        attachment filename resolution.
        """
        t0 = time.monotonic()
        timing: dict[str, float] = {"parse": 0.0}
        with self._pool.connection() as conn:
            parsed = self._resolve_account_names(conn, parsed)
            t = time.monotonic()
            fused = self._retrieve_pool(conn, parsed, candidates_per_arm, rerank_pool_size)
            timing["retrieve"] = (time.monotonic() - t) * 1000
            hydrated = self._hydrate(conn, fused)
        # Connection released. Reranker runs without holding a pool connection.
        t = time.monotonic()
        if self._reranker and hydrated:
            cap = self._cfg.rerank_max_chars
            snippets = [item["snippet_source_text"][:cap] for item in hydrated]
            scores = _safe_rerank(
                self._reranker,
                parsed.rewritten_text or parsed.free_text,
                snippets,
                fallback=[item["fused"].rrf_score for item in hydrated],
            )
        else:
            scores = [item["fused"].rrf_score for item in hydrated]
        timing["rerank"] = (time.monotonic() - t) * 1000
        needs_filename = any(
            item["fused"].best_chunk_table == "attachment_chunks" for item in hydrated
        )
        if needs_filename:
            with self._pool.connection() as conn:
                results = self._build_results(hydrated, parsed, scores, page=1,
                                              page_size=page_size, conn=conn,
                                              sort=sort)
        else:
            results = self._build_results(hydrated, parsed, scores, page=1,
                                          page_size=page_size, conn=None,
                                          sort=sort)
        timing["total"] = (time.monotonic() - t0) * 1000
        token = uuid.uuid4().hex[:16] if use_cache else None
        if token:
            self._cache.put(token, {
                "parsed": parsed, "hydrated": hydrated, "scores": scores,
                "candidates_per_arm": candidates_per_arm,
                "rerank_pool_size": rerank_pool_size, "page_size": page_size,
                "user_id": user_id,
                "sort": sort,
            })
        return SearchPage(
            results=results, page=1, page_size=page_size, pool_size=len(hydrated),
            candidates_per_arm=candidates_per_arm,
            has_more_in_pool=len(hydrated) > page_size, can_grow_pool=True,
            search_token=token, query=parsed, timing_ms=timing,
        )

    def search(
        self,
        query: str,
        *,
        page_size: int | None = None,
        candidates_per_arm: int | None = None,
        rerank_pool_size: int | None = None,
        use_cache: bool = True,
        smart: bool = False,
        disable_rerank: bool = False,
        user_id: int | None = None,
        sort: SortMode = "rank",
    ) -> SearchPage:
        """Run the full search pipeline and return page 1.

        `disable_rerank=True` short-circuits the cross-encoder and ranks by
        RRF score only. Useful for low-latency or debugging paths.

        `sort="date"` keeps the hybrid retrieval pool (same candidates as
        rank order) but re-sorts the page by
        ``COALESCE(internal_date, date_sent) DESC NULLS LAST``. The
        empty-query branch is already date-ordered, so the param has no
        effect there. ``continue_page`` honors whichever sort was used
        when the cursor was minted.
        """
        t0 = time.monotonic()
        cfg = self._cfg
        effective_page_size: int = min(page_size or cfg.page_size_default,
                                       cfg.page_size_max)
        cpa = candidates_per_arm or cfg.candidates_per_arm
        rps = rerank_pool_size or cfg.rerank_pool_size
        if smart and self._rewriter is None:
            raise RuntimeError("--smart requires a configured rewriter (Phase 4)")

        timing: dict[str, float] = {}
        t = time.monotonic()
        parsed = parse_query(query)
        timing["parse"] = (time.monotonic() - t) * 1000

        # Empty-query fallback: an empty `free_text` is the canonical
        # "show me my mail" signal. The hybrid pipeline degenerates badly
        # for it — BM25 arms early-return [] (no terms to match) and the
        # vector arms rank by cosine distance to the embedding of the empty
        # string, producing exactly `rerank_pool_size` (default 20)
        # arbitrary-looking hits. We short-circuit to a date-sorted list so
        # callers (GUI, MCP, programmatic API) get a predictable result —
        # structured filters still apply via `_filter_sql`.
        if not parsed.free_text.strip():
            t = time.monotonic()
            with self._pool.connection() as conn:
                parsed = self._resolve_account_names(conn, parsed)
                self._maybe_warn_unpopulated_body_lang(conn, parsed)
                results = self._list_recent_messages(conn, parsed, effective_page_size)
            timing["retrieve"] = (time.monotonic() - t) * 1000
            timing["rerank"] = 0.0
            timing["total"] = (time.monotonic() - t0) * 1000
            return SearchPage(
                results=results, page=1, page_size=effective_page_size,
                pool_size=len(results), candidates_per_arm=cpa,
                has_more_in_pool=False, can_grow_pool=False,
                search_token=None, query=parsed, timing_ms=timing,
            )

        with self._pool.connection() as conn:
            parsed = self._resolve_account_names(conn, parsed)
            self._maybe_warn_unpopulated_body_lang(conn, parsed)
            t = time.monotonic()
            fused = self._retrieve_pool(conn, parsed, cpa, rps)
            timing["retrieve"] = (time.monotonic() - t) * 1000
            hydrated = self._hydrate(conn, fused)
        # Connection released. Reranker runs without holding a pool connection.
        t = time.monotonic()
        reranker = None if disable_rerank else self._reranker
        if reranker is not None and hydrated:
            snippets_for_rerank = [
                item["snippet_source_text"][: cfg.rerank_max_chars]
                for item in hydrated
            ]
            scores = _safe_rerank(
                reranker,
                parsed.rewritten_text or parsed.free_text,
                snippets_for_rerank,
                fallback=[item["fused"].rrf_score for item in hydrated],
            )
        else:
            scores = [item["fused"].rrf_score for item in hydrated]
        timing["rerank"] = (time.monotonic() - t) * 1000

        needs_filename = any(
            item["fused"].best_chunk_table == "attachment_chunks" for item in hydrated
        )
        if needs_filename:
            with self._pool.connection() as conn:
                results = self._build_results(hydrated, parsed, scores, page=1,
                                              page_size=effective_page_size, conn=conn,
                                              sort=sort)
        else:
            results = self._build_results(hydrated, parsed, scores, page=1,
                                          page_size=effective_page_size, conn=None,
                                          sort=sort)
        timing["total"] = (time.monotonic() - t0) * 1000

        token: str | None = None
        if use_cache:
            token = uuid.uuid4().hex[:16]
            self._cache.put(token, {
                "parsed": parsed, "hydrated": hydrated, "scores": scores,
                "candidates_per_arm": cpa, "rerank_pool_size": rps,
                "page_size": effective_page_size,
                "user_id": user_id,
                "sort": sort,
            })
        pool_size = len(hydrated)
        return SearchPage(
            results=results, page=1, page_size=effective_page_size,
            pool_size=pool_size,
            candidates_per_arm=cpa,
            has_more_in_pool=pool_size > effective_page_size,
            can_grow_pool=True,
            search_token=token, query=parsed, timing_ms=timing,
        )
