# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Sweep `SearchConfig.rrf_k` against either acceptance corpus.

Seeds the chosen corpus and runs the extract/embed workers exactly once,
then re-runs the query suite for each candidate ``rrf_k`` against the same
pool of embedded chunks. Only the fusion step varies between sweeps, so the
comparison is apples-to-apples and the expensive embedding pass is not
repeated. Used to investigate issue #35 (whether arm 4 changes the
optimal ``rrf_k``).

Usage::

    LOCALMAIL_TEST_DSN=postgresql://... \\
      PYTHONPATH=src:. uv run python tests/acceptance/run_rrf_k_sweep.py \\
      --corpus multilingual \\
      --queries tests/fixtures/multilingual_queries.json \\
      --k 20 \\
      --rrf-ks 30,45,60,90

    # Attachment-corpus variant (exercises arm 4):
    PYTHONPATH=src:. uv run python tests/acceptance/run_rrf_k_sweep.py \\
      --corpus attachment \\
      --queries tests/fixtures/attachment_queries.json \\
      --k 20 \\
      --rrf-ks 30,45,60,90

Prints a per-(k, lang) table of recall@K + MRR@K, plus a mean across the
gated languages for each candidate ``rrf_k``. Set ``--candidates-per-arm``
below the default ``k*3`` to tighten the candidate pool and amplify fusion
sensitivity (use when recall is saturated at the default).

Prerequisites mirror ``run_recall_eval.py``: a reachable Postgres DSN +
the fastembed model in the local cache.
"""

from __future__ import annotations

import argparse
import json
import os
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
from localmail.search.lang_detect import make_detector, run_lang_detect_pass
from localmail.search.searcher import Searcher

from tests._attachment_corpus import build_corpus as build_attachment_corpus
from tests._multilingual_corpus import build_corpus as build_multilingual_corpus

# Phase-1 gated languages — the mean used to pick a winner is over these.
# The attachment corpus only ships English queries, so when it's selected the
# script falls back to all observed languages (see ``_gated_for_corpus``).
GATED_LANGS_MULTILINGUAL = ("de", "en", "es", "ja")
GATED_LANGS_ATTACHMENT = ("en",)


def _gated_for_corpus(corpus: str) -> tuple[str, ...]:
    if corpus == "attachment":
        return GATED_LANGS_ATTACHMENT
    return GATED_LANGS_MULTILINGUAL

# Truncate scope mirrors ``run_recall_eval.py`` / ``run_attachment_eval.py``
# so the sweep starts from the same blank slate as the existing harnesses.
_TRUNCATE_SQL_MULTILINGUAL = (
    "TRUNCATE accounts, mailboxes, messages, message_labels,"
    " attachment_blobs, failed_messages, message_chunks,"
    " failed_embeddings, embedding_models RESTART IDENTITY CASCADE"
)
_TRUNCATE_SQL_ATTACHMENT = (
    "TRUNCATE accounts, mailboxes, messages, message_labels,"
    " attachment_blobs, failed_messages, message_chunks,"
    " failed_embeddings, embedding_models, failed_chunkings,"
    " attachment_text, attachment_chunks, failed_extractions"
    " RESTART IDENTITY CASCADE"
)


def _reciprocal_rank(ordered_subjects: list[str], relevant: set[str]) -> float:
    for i, s in enumerate(ordered_subjects, start=1):
        if s in relevant:
            return 1.0 / i
    return 0.0


def _seed_and_embed_multilingual(
    conn: psycopg.Connection, cfg: SearchConfig,
) -> FastEmbedBackend:
    with conn.cursor() as cur:
        cur.execute(_TRUNCATE_SQL_MULTILINGUAL)
    conn.commit()
    build_multilingual_corpus(conn)

    backend = FastEmbedBackend(cfg)
    lang_detector = make_detector(cfg)

    print("Running embed worker …", file=sys.stderr)
    passes = 0
    while True:
        sweep = run_embed_worker_once(
            conn, cfg, backend, lang_detector=lang_detector,
        )
        passes += 1
        if not sweep.made_progress:
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
        print(f"  lang detect: {detected} message(s)", file=sys.stderr)

    return backend


def _seed_and_embed_attachment(
    conn: psycopg.Connection, cfg: SearchConfig, attachments_root: Path,
) -> FastEmbedBackend:
    with conn.cursor() as cur:
        cur.execute(_TRUNCATE_SQL_ATTACHMENT)
    conn.commit()
    build_attachment_corpus(conn, attachments_root=attachments_root)

    backend = FastEmbedBackend(cfg)

    print("Running extract worker …", file=sys.stderr)
    passes = 0
    while True:
        wrote = run_extract_worker_once(conn, cfg)
        passes += 1
        if wrote == 0:
            break
    print(f"  extract worker: {passes} pass(es)", file=sys.stderr)

    print("Running embed worker …", file=sys.stderr)
    passes = 0
    while True:
        sweep = run_embed_worker_once(conn, cfg, backend)
        passes += 1
        if not sweep.made_progress:
            break
    print(f"  embed worker: {passes} pass(es)", file=sys.stderr)

    return backend


def _evaluate_one(
    pool, cfg: SearchConfig, backend: FastEmbedBackend, queries: list[dict],
    k: int, candidates_per_arm: int,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return ({lang: mean_recall}, {lang: mean_mrr}) for one ``rrf_k``."""
    searcher = Searcher(
        pool=pool, cfg=cfg, embeddings=backend, reranker=None, rewriter=None,
    )
    per_lang_recall: dict[str, list[float]] = defaultdict(list)
    per_lang_mrr: dict[str, list[float]] = defaultdict(list)
    for q in queries:
        page = searcher.search(
            q["query"],
            allowed_account_ids=None,
            page_size=k,
            candidates_per_arm=candidates_per_arm,
            rerank_pool_size=k,
        )
        ranked: list[str] = [r.subject for r in page.results if r.subject is not None]
        relevant = set(q["relevant_subjects"])
        hits = len([s for s in ranked if s in relevant])
        per_lang_recall[q["lang"]].append(min(1.0, hits / max(1, len(relevant))))
        per_lang_mrr[q["lang"]].append(_reciprocal_rank(ranked, relevant))
    return (
        {lang: statistics.fmean(v) for lang, v in per_lang_recall.items()},
        {lang: statistics.fmean(v) for lang, v in per_lang_mrr.items()},
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep rrf_k against the recall harness.")
    ap.add_argument("--queries", required=True, type=Path)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument(
        "--corpus", choices=("multilingual", "attachment"), default="multilingual",
        help="Which synthetic corpus to seed before sweeping.",
    )
    ap.add_argument(
        "--rrf-ks", default="30,45,60,90",
        help="Comma-separated rrf_k values to sweep (default 30,45,60,90).",
    )
    ap.add_argument(
        "--candidates-per-arm", type=int, default=None,
        help="Override candidates_per_arm. Default is k*3 (matching the "
             "Phase-1 harness). Set tighter (e.g. k) to amplify fusion-sensitivity.",
    )
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    dsn = (
        args.dsn
        or os.environ.get("LOCALMAIL_TEST_DSN")
        or "postgresql://localmail:local%40%40mail@localhost:5532/localmail_test"
    )
    ks = [int(x.strip()) for x in args.rrf_ks.split(",") if x.strip()]
    if not ks:
        print("--rrf-ks must contain at least one integer", file=sys.stderr)
        return 2

    print(f"Applying migrations to {dsn!r} …", file=sys.stderr)
    apply_migrations(dsn)

    cfg_seed = SearchConfig()
    suite = json.loads(args.queries.read_text())
    queries = suite["queries"]

    attachments_tmpdir: tempfile.TemporaryDirectory | None = (
        tempfile.TemporaryDirectory() if args.corpus == "attachment" else None
    )
    try:
        if args.corpus == "multilingual":
            with psycopg.connect(dsn) as conn:
                backend = _seed_and_embed_multilingual(conn, cfg_seed)
        else:
            assert attachments_tmpdir is not None
            with psycopg.connect(dsn) as conn:
                backend = _seed_and_embed_attachment(
                    conn, cfg_seed, Path(attachments_tmpdir.name),
                )

        pool = open_pool(dsn)
        try:
            candidates = args.candidates_per_arm if args.candidates_per_arm else args.k * 3
            results: dict[int, tuple[dict[str, float], dict[str, float]]] = {}
            for k_value in ks:
                cfg = cfg_seed.model_copy(update={"rrf_k": k_value})
                results[k_value] = _evaluate_one(
                    pool, cfg, backend, queries, args.k, candidates,
                )
        finally:
            pool.close()
    finally:
        if attachments_tmpdir is not None:
            attachments_tmpdir.cleanup()

    gated = _gated_for_corpus(args.corpus)
    langs_seen = sorted({lang for recall, _ in results.values() for lang in recall})
    print(f"\n{'rrf_k':>6}  " + "  ".join(f"{lang:^14}" for lang in langs_seen)
          + f"  {'mean(gated)':>12}")
    print("-" * (8 + 16 * len(langs_seen) + 14))
    gated_means_by_k: dict[int, float] = {}
    for k_value in ks:
        recall, mrr = results[k_value]
        row = f"{k_value:>6}  "
        for lang in langs_seen:
            r = recall.get(lang, float("nan"))
            m = mrr.get(lang, float("nan"))
            row += f"R{r:.3f}/M{m:.3f}  "
        gated_means = [recall[lang] for lang in gated if lang in recall]
        gated_mean = statistics.fmean(gated_means) if gated_means else float("nan")
        gated_means_by_k[k_value] = gated_mean
        row += f"{gated_mean:>12.4f}"
        print(row)

    # Pick a "winner" only when the spread across rrf_k is above measurement
    # noise. Otherwise the sweep is saturated (one arm dominates fusion) and
    # printing a winner is misleading — call the tie out instead so readers
    # don't quote a meaningless leader.
    spread = max(gated_means_by_k.values()) - min(gated_means_by_k.values())
    if spread < 1e-4:
        any_mean = next(iter(gated_means_by_k.values()))
        print(f"\nAll rrf_k tied for mean recall@{args.k} across {gated} "
              f"(mean={any_mean:.4f}, spread<1e-4). Fusion is saturated.")
    else:
        best_k = max(gated_means_by_k, key=lambda k: gated_means_by_k[k])
        print(f"\nBest rrf_k by mean recall@{args.k} across {gated}: "
              f"{best_k} (mean={gated_means_by_k[best_k]:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
