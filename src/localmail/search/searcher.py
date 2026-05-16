"""Search engine orchestrator + pure helpers (RRF, snippets).

Most of this module is the Searcher class (Tasks 14–19); this commit
introduces only the data shapes and rrf_fuse so later tasks can build on
top.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

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
        )
        a1 = arm_bm25_messages(conn, parsed, self._cfg, limit=candidates_per_arm)
        a2 = arm_bm25_chunks(conn, parsed, self._cfg, limit=candidates_per_arm)
        qvec = self._embeddings.embed_query(parsed.rewritten_text or parsed.free_text)
        a3 = arm_vector_chunks(conn, parsed, self._cfg, qvec, limit=candidates_per_arm)
        fused = rrf_fuse([a1, a2, a3], k=self._cfg.rrf_k)
        return fused[:rerank_pool_size]

    def _hydrate(self, conn: psycopg.Connection, fused: list[FusedHit]) -> list[dict]:
        """Pull message + chunk text for each fused hit, returned in fused order."""
        if not fused:
            return []
        msg_ids = [h.message_id for h in fused]
        msgs: dict[int, dict] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, subject, from_addr, from_name, date_sent,"
                " body_text FROM messages WHERE id = ANY(%s)", (msg_ids,))
            for mid, acct, subj, fa, fn, ds, body in cur.fetchall():
                msgs[mid] = {"account_id": acct, "subject": subj, "from_addr": fa,
                             "from_name": fn, "date_sent": ds, "body_text": body}
            chunk_ids = [h.best_chunk_id for h in fused if h.best_chunk_id]
            chunks: dict[int, tuple[str, str]] = {}
            if chunk_ids:
                cur.execute(
                    "SELECT id, text, kind FROM message_chunks WHERE id = ANY(%s)",
                    (chunk_ids,),
                )
                chunks = {cid: (t, k) for cid, t, k in cur.fetchall()}
        out = []
        for h in fused:
            m = msgs.get(h.message_id, {})
            if h.best_chunk_id and h.best_chunk_id in chunks:
                snip_text, chunk_kind = chunks[h.best_chunk_id]
            else:
                snip_text = m.get("body_text") or ""
                chunk_kind = None
            out.append({
                "fused": h,
                "msg": m,
                "snippet_source_text": snip_text or "",
                "chunk_kind": chunk_kind,
            })
        return out

    def _build_results(
        self,
        hydrated: list[dict],
        parsed: ParsedQuery,
        rerank_scores: list[float],
        page: int,
        page_size: int,
    ) -> list[SearchResult]:
        terms = parsed.free_text.split()
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
            out.append(SearchResult(
                message_id=h.message_id, account_id=m.get("account_id", 0),
                rank=i, score=float(score), rrf_score=h.rrf_score,
                subject=m.get("subject"), from_addr=m.get("from_addr"),
                from_name=m.get("from_name"), date_sent=m.get("date_sent"),
                snippet=snip, snippet_source=source, attachment_filename=None,
                matched_chunk_id=h.best_chunk_id,
                matched_chunk_table=h.best_chunk_table,
            ))
        return out

    def continue_page(self, search_token: str, page: int) -> SearchPage:
        """Serve subsequent pages from the cached pool. Raises if past pool's end."""
        import math
        entry = self._cache.get(search_token)  # may raise CacheMissError
        page_size = entry["page_size"]
        pool_size = len(entry["hydrated"])
        max_page = max(1, math.ceil(pool_size / page_size))
        if page < 1 or page > max_page:
            raise PageOutOfPoolError(
                f"page {page} out of pool (pool_size={pool_size}, page_size={page_size}); "
                "call grow_pool to widen the candidate pool"
            )
        results = self._build_results(
            entry["hydrated"], entry["parsed"], entry["scores"], page, page_size,
        )
        return SearchPage(
            results=results, page=page, page_size=page_size, pool_size=pool_size,
            candidates_per_arm=entry["candidates_per_arm"],
            has_more_in_pool=pool_size > page * page_size,
            can_grow_pool=True,
            search_token=search_token, query=entry["parsed"],
            timing_ms={"cache_hit": 0.0},
        )

    def grow_pool(self, search_token: str, candidates_per_arm: int) -> SearchPage:
        """Re-run the pipeline with a larger candidate pool. Returns page 1."""
        entry = self._cache.get(search_token)
        parsed = entry["parsed"]
        self._cache.invalidate(search_token)
        # rerank pool grows proportionally so the larger arm output isn't wasted
        rps = max(candidates_per_arm, entry["rerank_pool_size"])
        page = self._search_with_parsed(parsed, page_size=entry["page_size"],
                                        candidates_per_arm=candidates_per_arm,
                                        rerank_pool_size=rps, use_cache=True)
        return page

    def _search_with_parsed(self, parsed, *, page_size, candidates_per_arm,
                            rerank_pool_size, use_cache):
        """Variant of search() that takes an already-parsed query."""
        t0 = time.monotonic()
        timing: dict[str, float] = {"parse": 0.0}
        with self._pool.connection() as conn:
            parsed = self._resolve_account_names(conn, parsed)
            t = time.monotonic()
            fused = self._retrieve_pool(conn, parsed, candidates_per_arm, rerank_pool_size)
            timing["retrieve"] = (time.monotonic() - t) * 1000
            hydrated = self._hydrate(conn, fused)
        t = time.monotonic()
        if self._reranker and hydrated:
            cap = self._cfg.rerank_max_chars
            snippets = [item["snippet_source_text"][:cap] for item in hydrated]
            scores = self._reranker.rerank(parsed.rewritten_text or parsed.free_text, snippets)
        else:
            scores = [item["fused"].rrf_score for item in hydrated]
        timing["rerank"] = (time.monotonic() - t) * 1000
        results = self._build_results(hydrated, parsed, scores, page=1, page_size=page_size)
        timing["total"] = (time.monotonic() - t0) * 1000
        token = uuid.uuid4().hex[:16] if use_cache else None
        if token:
            self._cache.put(token, {
                "parsed": parsed, "hydrated": hydrated, "scores": scores,
                "candidates_per_arm": candidates_per_arm,
                "rerank_pool_size": rerank_pool_size, "page_size": page_size,
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
    ) -> SearchPage:
        """Run the full search pipeline and return page 1.

        `disable_rerank=True` short-circuits the cross-encoder and ranks by
        RRF score only. Useful for low-latency or debugging paths.
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

        with self._pool.connection() as conn:
            parsed = self._resolve_account_names(conn, parsed)
            t = time.monotonic()
            fused = self._retrieve_pool(conn, parsed, cpa, rps)
            timing["retrieve"] = (time.monotonic() - t) * 1000
            hydrated = self._hydrate(conn, fused)

        t = time.monotonic()
        reranker = None if disable_rerank else self._reranker
        if reranker is not None and hydrated:
            snippets_for_rerank = [
                item["snippet_source_text"][: cfg.rerank_max_chars]
                for item in hydrated
            ]
            scores = reranker.rerank(
                parsed.rewritten_text or parsed.free_text, snippets_for_rerank,
            )
        else:
            scores = [item["fused"].rrf_score for item in hydrated]
        timing["rerank"] = (time.monotonic() - t) * 1000

        results = self._build_results(hydrated, parsed, scores, page=1,
                                      page_size=effective_page_size)
        timing["total"] = (time.monotonic() - t0) * 1000

        token: str | None = None
        if use_cache:
            token = uuid.uuid4().hex[:16]
            self._cache.put(token, {
                "parsed": parsed, "hydrated": hydrated, "scores": scores,
                "candidates_per_arm": cpa, "rerank_pool_size": rps,
                "page_size": effective_page_size,
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
