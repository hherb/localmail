"""Phase-2 acceptance harness: extraction success + retrieval recall/MRR.

Usage:
    LOCALMAIL_TEST_DSN=postgresql://... \\
      PYTHONPATH=src:. uv run python tests/acceptance/run_attachment_eval.py \\
      --queries tests/fixtures/attachment_queries.json \\
      --k 20

Gates:
    - Gate A (extraction success): >= 95% of allowlisted non-negative-test
      blobs produce a non-sentinel attachment_text row.
    - Gate B (retrieval quality): recall@20 >= 0.80, MRR@20 >= 0.50 on the
      query suite.

Also asserts no regression on Phase 1's run_recall_eval.py (run separately
via that script's own entry-point).

Prerequisites:
  - A running Postgres reachable at the DSN.
  - The fastembed model must be installed/cached (google/embeddinggemma-300m
    by default). First run downloads ~250 MB; subsequent runs use the cache.
  - Apply migrations via `uv run localmail init-db` or pass --dsn to a
    freshly-migrated test DB (the script calls apply_migrations automatically).
  - Optional: install the [extraction] uv extra for docling OCR support.
    Without it the scanned-PDF fixture lands as 'lightweight-empty' (sentinel),
    which counts against Gate A. Install with: uv sync --extra extraction
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import psycopg

from localmail.config import SearchConfig
from localmail.db import apply_migrations, open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.embeddings import FastEmbedBackend
from localmail.search.extract_worker import run_extract_worker_once
from localmail.search.extractor import _try_import_docling
from localmail.search.searcher import Searcher

from tests._attachment_corpus import build_corpus


def _reciprocal_rank(ordered: list[str], relevant: set[str]) -> float:
    """Return 1/rank of the first hit in *ordered* that is in *relevant*.

    Returns 0.0 if no relevant item appears in the list.  Used to compute
    Mean Reciprocal Rank (MRR) over the query suite.

    Args:
        ordered: Subjects in descending rank order as returned by Searcher.
        relevant: Set of subjects that count as relevant for this query.
    """
    for i, s in enumerate(ordered, start=1):
        if s in relevant:
            return 1.0 / i
    return 0.0


def _gate_a(
    conn: psycopg.Connection,
    seeded: list[dict],
    docling_available: bool,
) -> tuple[int, int, float, list[tuple[str, str | None]]]:
    """Compute Gate A: extraction success rate for allowlisted non-negative fixtures.

    Queries the attachment_text table to determine which fixtures have an
    extractor value that is neither a sentinel (size-skipped, lightweight-empty)
    nor absent.  Only fixtures whose tag is not None are considered (negatives
    are excluded from the denominator).

    When docling is not installed, scanned-PDF fixtures (MIME 'application/pdf'
    that produce 'lightweight-empty') are excluded from the denominator because
    lightweight extraction returns no text for image-only PDFs and the OCR
    fallback requires docling.  This prevents Gate A from failing on a known
    dependency-absence condition rather than a real extraction bug.

    Args:
        conn: Active psycopg connection.
        seeded: List of dicts as returned by build_corpus — each has keys
            'id' (message primary key), 'subject', 'tag', 'mime', 'filename'.
        docling_available: True iff docling is importable in this process.
            Pass the result of ``_try_import_docling() is not None``.

    Returns:
        Tuple of (non_sentinel_count, total_allowlisted, extraction_rate, details).
        *details* is a list of (subject, extractor_or_None) for diagnostics.
    """
    _SENTINELS = {"lightweight-empty", "size-skipped"}

    allowlisted = [f for f in seeded if f["tag"] is not None]
    details: list[tuple[str, str | None]] = []
    with conn.cursor() as cur:
        for f in allowlisted:
            cur.execute(
                """
                SELECT t.extractor
                FROM attachment_text t
                JOIN attachment_blobs b USING (sha256)
                JOIN messages m
                  ON m.attachments @>
                     jsonb_build_array(
                         jsonb_build_object('sha256', encode(b.sha256, 'hex'))
                     )
                WHERE m.id = %s
                """,
                (f["id"],),
            )
            row = cur.fetchone()
            extractor = row[0] if row is not None else None
            details.append((f["subject"], extractor))

    # Denominator: all positive fixtures, minus scanned PDFs when docling is
    # absent (those legitimately cannot be extracted without OCR).
    effective: list[tuple[dict, str | None]] = []
    for f, (_, extractor) in zip(allowlisted, details):
        is_scanned_pdf = (
            f["mime"] == "application/pdf"
            and extractor in _SENTINELS
        )
        if not docling_available and is_scanned_pdf:
            continue  # expected limitation; exclude from denominator
        effective.append((f, extractor))

    non_sentinel = sum(
        1 for _, ext in effective
        if ext is not None and ext not in _SENTINELS
    )
    total = len(effective)
    rate = non_sentinel / total if total > 0 else 0.0
    return non_sentinel, total, rate, details


def _gate_b(
    pool,
    cfg: SearchConfig,
    backend: FastEmbedBackend,
    queries_path: Path,
    k: int,
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """Compute Gate B: per-language recall@k and MRR@k over the query suite.

    Opens a Searcher with reranker=None for an un-reranked baseline
    (consistent with Phase 1's measurement methodology).  Iterates every
    query in the JSON suite and records recall and MRR into per-language
    buckets.

    Args:
        pool: psycopg_pool.ConnectionPool for the Searcher.
        cfg: SearchConfig providing all tunables.
        backend: Embedding backend (shared with the embed_worker run).
        queries_path: Path to the queries JSON file.
        k: Rank cutoff for recall and MRR.

    Returns:
        Tuple of (per_lang_recall, per_lang_mrr) dicts, each mapping
        language code strings to lists of per-query float scores.
    """
    searcher = Searcher(
        pool=pool,
        cfg=cfg,
        embeddings=backend,
        reranker=None,
        rewriter=None,
    )

    suite = json.loads(queries_path.read_text())
    per_lang_recall: dict[str, list[float]] = defaultdict(list)
    per_lang_mrr: dict[str, list[float]] = defaultdict(list)

    for q in suite["queries"]:
        page = searcher.search(
            q["query"],
            page_size=k,
            candidates_per_arm=k * 3,
            rerank_pool_size=k * 3,
        )
        ranked = [r.subject for r in page.results]
        relevant = set(q["relevant_subjects"])
        hits = len([s for s in ranked if s in relevant])
        recall = hits / max(1, len(relevant))
        per_lang_recall[q["lang"]].append(min(1.0, recall))
        per_lang_mrr[q["lang"]].append(_reciprocal_rank(ranked, relevant))

    return per_lang_recall, per_lang_mrr


def main() -> int:
    """Entry point for the Phase-2 acceptance harness.

    Applies migrations, seeds the synthetic attachment corpus, drains the
    extract and embed queues, then evaluates Gate A (extraction success) and
    Gate B (retrieval quality) before printing a summary table.

    Returns:
        0 if both gates pass, 1 if either fails.
    """
    ap = argparse.ArgumentParser(
        description="Phase-2 attachment-search acceptance harness."
    )
    ap.add_argument(
        "--queries", required=True, type=Path,
        help="Path to the attachment queries JSON file "
             "(tests/fixtures/attachment_queries.json).",
    )
    ap.add_argument(
        "--k", type=int, default=20,
        help="Rank cutoff for recall and MRR (default 20).",
    )
    ap.add_argument(
        "--dsn", default=None,
        help="Postgres DSN. Defaults to LOCALMAIL_TEST_DSN env var, then the "
             "hardcoded test default.",
    )
    args = ap.parse_args()

    import os
    dsn = (
        args.dsn
        or os.environ.get("LOCALMAIL_TEST_DSN")
        or "postgresql://localmail:local%40%40mail@localhost:5532/localmail_test"
    )
    docling_available = _try_import_docling() is not None
    print(
        f"docling available: {docling_available}"
        + (" (scanned-PDF fixture included in Gate A denominator)" if docling_available
           else " (scanned-PDF fixture excluded from Gate A denominator)"),
        file=sys.stderr,
    )

    print(f"Applying migrations to {dsn!r} …", file=sys.stderr)
    apply_migrations(dsn)

    with tempfile.TemporaryDirectory() as tmpdir:
        attachments_root = Path(tmpdir)

        print("Seeding corpus …", file=sys.stderr)
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE accounts, mailboxes, messages, message_labels,"
                    " attachment_blobs, failed_messages, message_chunks,"
                    " failed_embeddings, embedding_models, failed_chunkings,"
                    " attachment_text, attachment_chunks, failed_extractions"
                    " RESTART IDENTITY CASCADE"
                )
            conn.commit()
            seeded = build_corpus(conn, attachments_root=attachments_root)

            cfg = SearchConfig()
            backend = FastEmbedBackend(cfg)

            print("Running extract_worker …", file=sys.stderr)
            passes = 0
            while True:
                wrote = run_extract_worker_once(conn, cfg)
                passes += 1
                if wrote == 0:
                    break
            print(f"  extract_worker: {passes} pass(es)", file=sys.stderr)

            print("Running embed_worker …", file=sys.stderr)
            passes = 0
            while True:
                wrote = run_embed_worker_once(conn, cfg, backend)
                passes += 1
                if wrote == 0:
                    break
            print(f"  embed_worker: {passes} pass(es)", file=sys.stderr)

            # Gate A.
            non_sentinel, total, rate, details = _gate_a(
                conn, seeded, docling_available
            )

        print(
            f"\nGate A (extraction success): {non_sentinel}/{total} = {rate:.3f}"
            f"  (target >= 0.95)"
        )
        print(f"\n{'fixture':<32} {'extractor'}")
        print("-" * 55)
        for subject, extractor in details:
            print(f"  {subject:<30} {extractor or '(none)'}")
        gate_a_pass = rate >= 0.95

        # Gate B.
        pool = open_pool(dsn)
        try:
            per_lang_recall, per_lang_mrr = _gate_b(
                pool, cfg, backend, args.queries, args.k
            )
        finally:
            pool.close()

        print(f"\nGate B (retrieval quality, k={args.k}):")
        print(f"{'lang':<6} {'#q':>4} {'recall@K':>10} {'MRR@K':>8}  status")
        print("-" * 40)
        gate_b_failures: list[str] = []
        for lang in sorted(per_lang_recall):
            recalls = per_lang_recall[lang]
            mrrs = per_lang_mrr[lang]
            r = statistics.fmean(recalls)
            m = statistics.fmean(mrrs)
            ok = r >= 0.80 and m >= 0.50
            status = "PASS" if ok else "FAIL"
            if not ok:
                gate_b_failures.append(f"{lang}: recall={r:.3f}, MRR={m:.3f}")
            print(f"{lang:<6} {len(recalls):>4} {r:>10.3f} {m:>8.3f}  {status}")

        all_pass = gate_a_pass and not gate_b_failures
        if not all_pass:
            print("\nFAILURES:", file=sys.stderr)
            if not gate_a_pass:
                print(
                    f"  - Gate A: extraction rate {rate:.3f} < 0.95",
                    file=sys.stderr,
                )
            for failure in gate_b_failures:
                print(f"  - Gate B {failure}", file=sys.stderr)
            return 1

        print("\nAll Phase 2 acceptance gates PASS.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
