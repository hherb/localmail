"""SQL retrieval arms for Phase 1: BM25 (messages, chunks) + vector (chunks).

Each arm is a pure function `(conn, parsed_query, cfg, ...) -> list[ArmHit]`.
Arm 4 (vector over attachment_chunks) lands in Phase 2.

Arms 1 and 2 use PostgreSQL tsvector FTS with ts_rank_cd for scoring.
Arm 3 uses pgvector cosine distance over halfvec embeddings.
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
    if filters.label:
        parts.append(
            "EXISTS (SELECT 1 FROM message_labels ml JOIN mailboxes mb ON mb.id = ml.mailbox_id"
            " WHERE ml.message_id = m.id AND mb.name ILIKE %s)"
        )
        params.append(filters.label)
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
    with conn.cursor() as cur:
        # SET LOCAL requires an open transaction. psycopg_pool connections
        # are non-autocommit by default; if you ever wrap this call in an
        # autocommit context, the SET silently has no effect.
        cur.execute(f"SET LOCAL hnsw.ef_search = {int(cfg.hnsw_ef_search)}")
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        ArmHit(message_id=mid, chunk_id=cid, chunk_table="message_chunks",
               arm_score=float(score), rank=int(rank))
        for mid, cid, score, rank in rows
    ]
