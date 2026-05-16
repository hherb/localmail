"""Search engine orchestrator + pure helpers (RRF, snippets).

Most of this module is the Searcher class (Tasks 14–19); this commit
introduces only the data shapes and rrf_fuse so later tasks can build on
top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


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
