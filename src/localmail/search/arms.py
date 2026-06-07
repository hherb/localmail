"""SQL retrieval arms for hybrid search: BM25 (messages, chunks) + vector (chunks, attachment_chunks).

Each arm is a pure function `(conn, parsed_query, cfg, ...) -> list[ArmHit]`.

Arms 1 and 2 use PostgreSQL tsvector FTS with ts_rank_cd for scoring.
Arm 3 uses pgvector cosine distance over halfvec embeddings on message_chunks.
Arm 4 uses pgvector cosine distance over halfvec embeddings on attachment_chunks,
    joined to messages via JSONB containment on messages.attachments.
"""

from __future__ import annotations

from typing import Any

import psycopg

from localmail.config import SearchConfig
from localmail.search.query import ParsedQuery, SearchFilters
from localmail.search.searcher import ArmHit


def _filter_sql(filters: SearchFilters) -> tuple[str, list[Any]]:
    """Build a WHERE clause fragment + parameter list from SearchFilters.

    Returns (" AND ...sql...", [params]) or ("", []) if no filters.
    Callers splice the result directly after their existing WHERE clause.
    """
    parts: list[str] = []
    params: list[Any] = []
    if filters.accounts:
        parts.append("m.account_id = ANY(%s)")
        params.append(filters.accounts)
    if filters.account_ids:
        parts.append("m.account_id = ANY(%s)")
        params.append(filters.account_ids)
    # The date inclusivity (after >=, before <) and the substring ILIKE
    # operators below are an agent-facing contract documented in the MCP
    # `search` tool's Field descriptions (mcp/server.py). Changing an operator
    # here means updating that prose — the coupling is pinned by
    # tests/test_mcp_filter_semantics.py.
    if filters.from_substr:
        parts.append("(m.from_addr ILIKE %s OR m.from_name ILIKE %s)")
        like = f"%{filters.from_substr}%"
        params.extend([like, like])
    if filters.to_substr:
        parts.append("EXISTS (SELECT 1 FROM unnest(m.to_addrs) t WHERE t ILIKE %s)")
        params.append(f"%{filters.to_substr}%")
    if filters.subject_substr:
        parts.append("m.subject ILIKE %s")
        params.append(f"%{filters.subject_substr}%")
    if filters.after:
        parts.append("m.date_sent >= %s")
        params.append(filters.after)
    if filters.before:
        parts.append("m.date_sent < %s")
        params.append(filters.before)
    if filters.has_attachment is True:
        parts.append("jsonb_array_length(m.attachments) > 0")
    if filters.has_attachment is False:
        parts.append("jsonb_array_length(m.attachments) = 0")
    if filters.folders:
        parts.append(
            "EXISTS (SELECT 1 FROM message_labels ml JOIN mailboxes mb ON mb.id = ml.mailbox_id"
            " WHERE ml.message_id = m.id AND mb.name = ANY(%s))"
        )
        params.append(filters.folders)
    if filters.folder_ids:
        # No join to mailboxes needed — we have the PKs directly.
        parts.append(
            "EXISTS (SELECT 1 FROM message_labels ml"
            " WHERE ml.message_id = m.id AND ml.mailbox_id = ANY(%s))"
        )
        params.append(filters.folder_ids)
    if filters.label:
        parts.append(
            "EXISTS (SELECT 1 FROM message_labels ml JOIN mailboxes mb ON mb.id = ml.mailbox_id"
            " WHERE ml.message_id = m.id AND mb.name ILIKE %s)"
        )
        params.append(filters.label)
    if filters.languages:
        # `messages.body_lang` is populated per-message by language detection
        # (migration 0015). NULL rows are excluded — lang filtering is opt-in.
        # The leading `IS NOT NULL` matches the partial index predicate so
        # the planner uses `messages_body_lang_idx` even on tables where the
        # column is sparsely populated.
        parts.append("m.body_lang IS NOT NULL AND m.body_lang = ANY(%s)")
        params.append(list(filters.languages))
    if not parts:
        return "", []
    return " AND " + " AND ".join(parts), params


def arm_bm25_messages(
    conn: psycopg.Connection,
    parsed: ParsedQuery,
    cfg: SearchConfig,
    limit: int,
) -> list[ArmHit]:
    """tsvector FTS over messages.fts_v2 with per-field boosts via ts_rank_cd.

    Field weight order for ts_rank_cd float4[] is [D, C, B, A]:
      D = to_addrs, C = body, B = from, A = subject
    """
    if not parsed.free_text.strip():
        return []
    boosts = cfg.bm25_field_boosts
    raw = [
        boosts.get("to", 0.5),
        boosts.get("body", 1.0),
        boosts.get("from", 2.0),
        boosts.get("subject", 3.0),
    ]
    # ts_rank_cd requires all weights in [0, 1]; normalize by the max value.
    # If all boosts are <= 0 (degenerate config), fall back to all-ones so
    # the arm still ranks by match strength rather than returning 0 for
    # every row.
    max_w = max(raw)
    weights = [1.0] * len(raw) if max_w <= 0 else [w / max_w for w in raw]
    where_extra, where_params = _filter_sql(parsed.filters)
    sql = f"""
        WITH ranked AS (
            SELECT m.id,
                   ts_rank_cd(%s::float4[], m.fts_v2, plainto_tsquery('simple', %s)) AS score
            FROM messages m
            WHERE m.fts_v2 @@ plainto_tsquery('simple', %s)
            {where_extra}
            ORDER BY score DESC
            LIMIT %s
        )
        SELECT id, score, ROW_NUMBER() OVER (ORDER BY score DESC) FROM ranked
    """
    params: list[Any] = [weights, parsed.free_text, parsed.free_text, *where_params, limit]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        ArmHit(message_id=mid, chunk_id=None, chunk_table="message",
               arm_score=float(score), rank=int(rank))
        for mid, score, rank in rows
    ]


def arm_bm25_chunks(
    conn: psycopg.Connection,
    parsed: ParsedQuery,
    cfg: SearchConfig,
    limit: int,
) -> list[ArmHit]:
    """tsvector FTS over message_chunks.fts with default ts_rank_cd weights."""
    if not parsed.free_text.strip():
        return []
    where_extra, where_params = _filter_sql(parsed.filters)
    sql = f"""
        WITH ranked AS (
            SELECT mc.message_id, mc.id AS chunk_id,
                   ts_rank_cd(mc.fts, plainto_tsquery('simple', %s)) AS score
            FROM message_chunks mc JOIN messages m ON m.id = mc.message_id
            WHERE mc.fts @@ plainto_tsquery('simple', %s)
            {where_extra}
            ORDER BY score DESC
            LIMIT %s
        )
        SELECT message_id, chunk_id, score,
               ROW_NUMBER() OVER (ORDER BY score DESC) FROM ranked
    """
    params: list[Any] = [parsed.free_text, parsed.free_text, *where_params, limit]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        ArmHit(message_id=mid, chunk_id=cid, chunk_table="message_chunks",
               arm_score=float(score), rank=int(rank))
        for mid, cid, score, rank in rows
    ]


def arm_vector_chunks(
    conn: psycopg.Connection,
    parsed: ParsedQuery,
    cfg: SearchConfig,
    query_vector: list[float],
    limit: int,
) -> list[ArmHit]:
    """Cosine-distance vector search over message_chunks.embedding_v1."""
    where_extra, where_params = _filter_sql(parsed.filters)
    sql = f"""
        WITH ranked AS (
            SELECT mc.message_id, mc.id AS chunk_id,
                   1.0 - (mc.embedding_v1 <=> %s::halfvec) AS score
            FROM message_chunks mc JOIN messages m ON m.id = mc.message_id
            WHERE mc.embedding_v1 IS NOT NULL
            {where_extra}
            ORDER BY mc.embedding_v1 <=> %s::halfvec
            LIMIT %s
        )
        SELECT message_id, chunk_id, score,
               ROW_NUMBER() OVER (ORDER BY score DESC) FROM ranked
    """
    params: list[Any] = [query_vector, *where_params, query_vector, limit]
    # ef_search must be >= the LIMIT we want returned, otherwise HNSW
    # truncates the candidate set before our SELECT sees it.
    ef = max(int(cfg.hnsw_ef_search), int(limit))
    with conn.cursor() as cur:
        # SET LOCAL requires an open transaction. psycopg_pool connections
        # are non-autocommit by default; if you ever wrap this call in an
        # autocommit context, the SET silently has no effect.
        cur.execute(f"SET LOCAL hnsw.ef_search = {ef}")
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        ArmHit(message_id=mid, chunk_id=cid, chunk_table="message_chunks",
               arm_score=float(score), rank=int(rank))
        for mid, cid, score, rank in rows
    ]


def arm_vector_attachment_chunks(
    conn: psycopg.Connection,
    parsed: ParsedQuery,
    cfg: SearchConfig,
    qvec: list[float],
    limit: int,
) -> list[ArmHit]:
    """Arm 4 — vector cosine over attachment_chunks, JOINed to messages via
    JSONB containment on messages.attachments.

    Process:
        1. Rank attachment chunks by cosine distance from qvec using the HNSW
           index on attachment_chunks.embedding_v1. A wider candidate set of
           limit * 3 chunks is fetched first to give fan-out headroom — a
           single high-scoring chunk may expand to many messages, so the
           raw chunk count needed before fan-out capping is larger than limit.
        2. JOIN each matched chunk to every message that references the
           carrying blob. The join condition uses the JSONB @> containment
           operator on messages.attachments, which is accelerated by the GIN
           index (migration 0013). encode(sha256, 'hex') converts the BYTEA
           primary key to the hex string stored in the JSONB.
        3. Cap fan-out per chunk at cfg.arm4_fanout_cap using ROW_NUMBER()
           PARTITION BY chunk id, ordered by messages.date_sent DESC NULLS
           LAST so the most recent carriers win when fan-out exceeds the cap.
           This prevents a single popular blob (e.g. a newsletter PDF attached
           to hundreds of recipients) from monopolising the candidate budget.
        4. Apply Phase 1 filter SQL (account:, folder:, after:, before:,
           from:, to:, subject:, label:, has:attachment) via _filter_sql().
           Filters operate on the messages alias 'm', matching every other arm.
        5. Convert cosine distance [0, 2] to arm_score via 1.0 - dist,
           yielding a similarity-like value in [-1, 1]. RRF fusion uses only
           the rank order, not absolute scores, so the conversion is for
           interpretability rather than correctness.

    Returns up to `limit` ArmHits, each with chunk_table='attachment_chunks'
    and rank in [1, limit].
    """
    filter_sql, filter_params = _filter_sql(parsed.filters)
    # Fetch 3x the requested limit of chunks before fan-out so that after
    # expanding each chunk to its carrying messages and applying the per-chunk
    # cap, there are still enough candidates to fill the output budget.
    chunk_limit = max(limit, 1) * cfg.arm4_chunk_prefetch_multiplier

    sql = f"""
    WITH ranked_chunks AS (
        SELECT c.id      AS chunk_id,
               c.sha256  AS chunk_sha256,
               c.embedding_v1 <=> %s::halfvec(768) AS dist
        FROM attachment_chunks c
        WHERE c.embedding_v1 IS NOT NULL
        ORDER BY c.embedding_v1 <=> %s::halfvec(768)
        LIMIT %s
    ),
    fanned AS (
        SELECT m.id                                              AS message_id,
               rc.chunk_id,
               rc.dist,
               ROW_NUMBER() OVER (
                   PARTITION BY rc.chunk_id
                   ORDER BY m.date_sent DESC NULLS LAST
               )                                                AS rn
        FROM ranked_chunks rc
        JOIN messages m
          ON m.attachments @> jsonb_build_array(
                 jsonb_build_object('sha256', encode(rc.chunk_sha256, 'hex'))
             )
        WHERE TRUE
          {filter_sql}
    )
    SELECT message_id, chunk_id, dist
    FROM fanned
    WHERE rn <= %s
    ORDER BY dist
    LIMIT %s
    """

    params: list[Any] = [
        qvec, qvec, chunk_limit,
        *filter_params,
        cfg.arm4_fanout_cap, limit,
    ]

    # ef_search must be >= chunk_limit; otherwise HNSW returns fewer
    # candidates than the prefetch ORDER BY ... LIMIT expects, capping the
    # fan-out budget below what the caller actually requested.
    ef = max(int(cfg.hnsw_ef_search), int(chunk_limit))
    with conn.cursor() as cur:
        cur.execute(f"SET LOCAL hnsw.ef_search = {ef}")
        cur.execute(sql, params)
        rows = cur.fetchall()

    out: list[ArmHit] = []
    for rank, (message_id, chunk_id, dist) in enumerate(rows, start=1):
        arm_score = float(1.0 - dist)
        out.append(
            ArmHit(
                message_id=message_id,
                chunk_id=chunk_id,
                chunk_table="attachment_chunks",
                rank=rank,
                arm_score=arm_score,
            )
        )
    return out
