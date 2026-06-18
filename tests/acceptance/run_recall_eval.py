# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Recall@K + MRR@K eval harness for the Phase-1 multilingual acceptance suite.

Usage:
    LOCALMAIL_TEST_DSN=postgresql://... \\
      PYTHONPATH=src:. uv run python tests/acceptance/run_recall_eval.py \\
      --queries tests/fixtures/multilingual_queries.json \\
      --k 20

Prints a per-language summary and an overall pass/fail against the
Phase-1 targets (recall@20 >= 80% and MRR@20 >= 0.5 for de/en/es/ja;
Norwegian reported but not gated).

Prerequisites:
  - A running Postgres reachable at the DSN.
  - The fastembed model must be installed/cached (google/embeddinggemma-300m
    by default). First run downloads ~250 MB; subsequent runs use the cache.
  - Apply migrations via `uv run localmail init-db` or pass --dsn to a
    freshly-migrated test DB (the script calls apply_migrations automatically).

Note on the smoke run: if the embedding model is not yet in the fastembed
registry / cache, the script will fail during the embed_worker pass. This
is a known Phase 1 deferred concern (fastembed model registry issue). To
verify the harness without the model, mock FastEmbedBackend.embed_documents
to return zero vectors of the right dimension and re-run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import psycopg

from localmail.config import SearchConfig
from localmail.db import apply_migrations, open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.embeddings import FastEmbedBackend
from localmail.search.lang_detect import make_detector, run_lang_detect_pass
from localmail.search.searcher import Searcher

from tests._multilingual_corpus import build_corpus

# Phase-1 acceptance gates: recall@K >= target[0], MRR@K >= target[1].
# Norwegian is reported but not gated (vocabulary frugality makes it OK for now).
TARGETS = {
    "de": (0.80, 0.50),
    "en": (0.80, 0.50),
    "es": (0.80, 0.50),
    "ja": (0.80, 0.50),
}


def _reciprocal_rank(ordered_subjects: list[str], relevant: set[str]) -> float:
    for i, s in enumerate(ordered_subjects, start=1):
        if s in relevant:
            return 1.0 / i
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run Phase-1 multilingual recall + MRR evaluation."
    )
    ap.add_argument(
        "--queries", required=True, type=Path,
        help="Path to the queries JSON file (see tests/fixtures/multilingual_queries.example.json).",
    )
    ap.add_argument(
        "--k", type=int, default=20,
        help="Depth at which recall and MRR are computed (default 20).",
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

    print(f"Applying migrations to {dsn!r} …", file=sys.stderr)
    apply_migrations(dsn)

    print("Seeding corpus …", file=sys.stderr)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE accounts, mailboxes, messages, message_labels,"
                " attachment_blobs, failed_messages, message_chunks,"
                " failed_embeddings, embedding_models RESTART IDENTITY CASCADE"
            )
        conn.commit()
        seeded = build_corpus(conn)

        cfg = SearchConfig()
        backend = FastEmbedBackend(cfg)
        lang_detector = make_detector(cfg)
        print("Running embed worker …", file=sys.stderr)
        passes = 0
        while True:
            wrote = run_embed_worker_once(
                conn, cfg, backend, lang_detector=lang_detector,
            )
            passes += 1
            if wrote == 0:
                break
        print(f"  embed worker: {passes} pass(es)", file=sys.stderr)

        if lang_detector is not None:
            print("Running language detection pass …", file=sys.stderr)
            detected = 0
            while True:
                n = run_lang_detect_pass(conn, cfg, lang_detector)
                if n == 0:
                    break
                detected += n
            with conn.cursor() as cur:
                cur.execute("SELECT body_lang, count(*) FROM messages"
                            " GROUP BY body_lang ORDER BY 2 DESC")
                breakdown = cur.fetchall()
            print(f"  lang detect: {detected} message(s), breakdown {breakdown}",
                  file=sys.stderr)

    pool = open_pool(dsn)
    try:
        searcher = Searcher(
            pool=pool,
            cfg=cfg,
            embeddings=backend,
            reranker=None,   # Phase-1 gate is un-reranked baseline
            rewriter=None,
        )

        suite = json.loads(args.queries.read_text())
        per_lang_recall: dict[str, list[float]] = defaultdict(list)
        per_lang_mrr: dict[str, list[float]] = defaultdict(list)

        for q in suite["queries"]:
            page = searcher.search(
                q["query"],
                page_size=args.k,
                candidates_per_arm=args.k * 3,
                rerank_pool_size=args.k * 3,
            )
            ranked = [r.subject for r in page.results]
            relevant = set(q["relevant_subjects"])
            hits = len([s for s in ranked if s in relevant])
            recall = hits / max(1, len(relevant))
            per_lang_recall[q["lang"]].append(min(1.0, recall))
            per_lang_mrr[q["lang"]].append(_reciprocal_rank(ranked, relevant))
    finally:
        pool.close()

    failures: list[str] = []
    print(f"\n{'lang':<6} {'#q':>4} {'recall@K':>10} {'MRR@K':>8}  status")
    print("-" * 40)
    for lang in sorted({*per_lang_recall, *per_lang_mrr}):
        recalls = per_lang_recall[lang]
        mrrs = per_lang_mrr[lang]
        r = statistics.fmean(recalls)
        m = statistics.fmean(mrrs)
        target = TARGETS.get(lang)
        status = "—"
        if target:
            ok = r >= target[0] and m >= target[1]
            status = "PASS" if ok else "FAIL"
            if not ok:
                failures.append(
                    f"{lang}: recall={r:.3f} (need {target[0]}),"
                    f" MRR={m:.3f} (need {target[1]})"
                )
        print(f"{lang:<6} {len(recalls):>4} {r:>10.3f} {m:>8.3f}  {status}")

    if failures:
        print("\nFAILURES (Phase 1 acceptance gates not met):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("\nAll gated languages PASS Phase 1 acceptance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
