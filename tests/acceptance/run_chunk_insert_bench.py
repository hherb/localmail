# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Benchmark the chunking-loop INSERT strategy (issue #5).

`embed_worker._chunk_messages_lazily` INSERTs message chunks row-by-row (one
``cur.execute`` per chunk). #5 asked whether batching each message's chunks
into a single ``cur.executemany`` (inside the same per-message SAVEPOINT, so
poison isolation is unchanged) is worth it on a large-archive backfill.

This operator-facing harness produces the measurement #5 asked for ("defer
until someone actually measures backfill time on a large archive"): it seeds N
multi-chunk messages into ``LOCALMAIL_TEST_DSN`` and times both strategies
against an identical seed, reporting chunks/sec and the speedup.

**Finding (2026-06, localhost Postgres, single-host deployment):** the loop is
**tokenization-bound** — throughput is ~constant (~880 chunks/s) regardless of
INSERT strategy because ``chunk_message`` spends its time in tiktoken
``encode``, not in INSERT round-trips. On localhost ``executemany`` is in fact
~4% *slower* (per-call batching overhead with no round-trip latency to amortise
away). localmail is explicitly single-host, so Postgres is always local and the
remote-DB scenario where ``executemany`` would win does not apply. #5 was closed
on this evidence; the production loop stays row-by-row.

Both strategies are defined locally here (the production code carries neither an
``executemany`` form nor a param-builder helper) so this remains a reproducible,
self-contained measurement — the same pattern ``run_browse_explain.py`` uses for
its ``pre75`` predicate variant.

Usage::

    LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test \\
      PYTHONPATH=src:. uv run python tests/acceptance/run_chunk_insert_bench.py \\
      --messages 5000 --mode both

Prerequisites: a reachable Postgres at ``LOCALMAIL_TEST_DSN`` with the schema
migrated. The harness TRUNCATEs accounts/messages/message_chunks/failed_chunkings
before each timed run (it never touches the live ``localmail`` database — the
DSN defaults to ``localmail_test``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import psycopg

from localmail.config import SearchConfig
from localmail.db import apply_migrations
from localmail.search.chunking import ChunkSpec, MessageRow, chunk_message
from localmail.search.embed_worker import record_failed_chunking
from tests.acceptance._harness_lock import checkpoint, harness_db_lock

# Both INSERT strategies share this SQL; only the call shape (execute vs
# executemany) differs between the two timed variants below.
_MESSAGE_CHUNK_INSERT = (
    "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
    " VALUES (%s, %s, %s, %s, %s)"
    " ON CONFLICT (message_id, kind, chunk_idx) DO NOTHING"
)

# The SELECT that finds unchunked messages — identical to the production
# `_chunk_messages_lazily` query so the benchmark exercises the same plan.
_SELECT_UNCHUNKED = """
SELECT m.id, m.subject, m.from_addr, m.from_name, m.to_addrs,
       m.date_sent, m.body_text
FROM messages m
LEFT JOIN message_chunks mc ON mc.message_id = m.id
WHERE mc.id IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM failed_chunkings fc
      WHERE fc.message_id = m.id
        AND fc.retry_count >= %s
  )
ORDER BY m.id
LIMIT %s
FOR UPDATE OF m SKIP LOCKED
"""


def _message_chunk_params(message_id: int, specs: list[ChunkSpec]) -> list[tuple]:
    """Map a message_id + its chunk specs to executemany parameter tuples."""
    return [
        (message_id, s.kind, s.chunk_idx, s.text, s.token_count) for s in specs
    ]


def _chunk_executemany(conn: psycopg.Connection, cfg: SearchConfig, batch: int) -> int:
    """Candidate variant: one executemany per message inside its SAVEPOINT."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_UNCHUNKED, (cfg.embed_worker_max_chunk_retries, batch))
        rows = cur.fetchall()
        if not rows:
            return 0
        for mid, subj, fa, fn, to, ds, body in rows:
            cur.execute("SAVEPOINT msg")
            try:
                msg = MessageRow(id=mid, subject=subj, from_addr=fa, from_name=fn,
                                 to_addrs=to, date_sent=ds, body_text=body)
                cur.executemany(
                    _MESSAGE_CHUNK_INSERT,
                    _message_chunk_params(mid, chunk_message(msg, cfg)),
                )
                cur.execute("RELEASE SAVEPOINT msg")
            except Exception as exc:  # noqa: BLE001 — poison-pill isolation
                cur.execute("ROLLBACK TO SAVEPOINT msg")
                record_failed_chunking(conn, mid, exc)
    conn.commit()
    return len(rows)

# A body long enough to split into several body chunks on top of the header
# chunk, so each message drives multiple INSERTs (the case #5 cares about).
_WORD = "lorem ipsum dolor sit amet consectetur adipiscing elit "
DEFAULT_BODY_WORDS = 600
DEFAULT_MESSAGES = 5000
TRUNCATE_SQL = (
    "TRUNCATE accounts, messages, message_chunks, failed_chunkings"
    " RESTART IDENTITY CASCADE"
)


def _seed_messages(conn: psycopg.Connection, n: int, body: str) -> None:
    """Insert one account + ``n`` messages with the given body. Chunks are NOT
    created — that's the work being benchmarked."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('bench', 'bench@x', 'h', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        with cur.copy(
            "COPY messages (account_id, message_id, raw_sha256, subject,"
            " from_addr, body_text, headers, raw_bytes, size_bytes)"
            " FROM STDIN"
        ) as copy:
            for i in range(n):
                copy.write_row((
                    acct,
                    f"<bench-{i}@x>",
                    bytes([i & 0xFF]) + b"\x00" * 31,
                    f"subject {i}",
                    "sender@x",
                    body,
                    "{}",
                    b"raw",
                    len(body),
                ))
    conn.commit()


def _chunk_row_by_row(
    conn: psycopg.Connection, cfg: SearchConfig, batch: int
) -> int:
    """Production variant: per-message SAVEPOINT, one ``cur.execute`` per chunk.

    Mirrors the shipped ``_chunk_messages_lazily`` body so the benchmark compares
    like for like (the SELECT + SAVEPOINT scaffolding is shared with
    ``_chunk_executemany``; only the INSERT call shape differs).
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_UNCHUNKED, (cfg.embed_worker_max_chunk_retries, batch))
        rows = cur.fetchall()
        if not rows:
            return 0
        for mid, subj, fa, fn, to, ds, body in rows:
            cur.execute("SAVEPOINT msg")
            try:
                msg = MessageRow(id=mid, subject=subj, from_addr=fa, from_name=fn,
                                 to_addrs=to, date_sent=ds, body_text=body)
                for spec in chunk_message(msg, cfg):
                    cur.execute(
                        _MESSAGE_CHUNK_INSERT,
                        (mid, spec.kind, spec.chunk_idx, spec.text, spec.token_count),
                    )
                cur.execute("RELEASE SAVEPOINT msg")
            except Exception as exc:  # noqa: BLE001 — poison-pill isolation
                cur.execute("ROLLBACK TO SAVEPOINT msg")
                record_failed_chunking(conn, mid, exc)
    conn.commit()
    return len(rows)


def _run_mode(
    dsn: str, lock: object, mode: str, n: int, body: str, cfg: SearchConfig
) -> dict:
    """TRUNCATE, seed ``n`` messages, time the chosen chunking strategy."""
    fn = _chunk_executemany if mode == "executemany" else _chunk_row_by_row
    # On the default `--mode both` the second call lands after a complete
    # benchmark run, so the lock is re-checked before the truncate rather
    # than assumed to have survived it.
    checkpoint(lock)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(TRUNCATE_SQL)
        conn.commit()
        _seed_messages(conn, n, body)
        start = time.perf_counter()
        processed = fn(conn, cfg, batch=n)
        elapsed = time.perf_counter() - start
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM message_chunks")
            chunks = cur.fetchone()[0]
    return {
        "mode": mode,
        "messages": processed,
        "chunks_inserted": chunks,
        "elapsed_s": round(elapsed, 4),
        "chunks_per_s": round(chunks / elapsed, 1) if elapsed else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=DEFAULT_MESSAGES)
    parser.add_argument("--body-words", type=int, default=DEFAULT_BODY_WORDS)
    parser.add_argument(
        "--mode", choices=("row", "executemany", "both"), default="both"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "LOCALMAIL_TEST_DSN",
            "postgresql://localmail:local%40%40mail@localhost:5532/localmail_test",
        ),
    )
    args = parser.parse_args()

    with harness_db_lock(args.dsn) as lock:
        apply_migrations(args.dsn)
        body = (_WORD * args.body_words).strip()
        cfg = SearchConfig()
        modes = ("row", "executemany") if args.mode == "both" else (args.mode,)
        results = [
            _run_mode(args.dsn, lock, m, args.messages, body, cfg) for m in modes
        ]

        if args.json:
            print(json.dumps({"results": results}, indent=2))
            return 0

        print(f"chunk-insert benchmark: {args.messages} messages, "
              f"body~{args.body_words} words")
        for r in results:
            print(f"  {r['mode']:>11}: {r['chunks_inserted']} chunks in "
                  f"{r['elapsed_s']}s  ({r['chunks_per_s']} chunks/s)")
        by_mode = {r["mode"]: r for r in results}
        if "row" in by_mode and "executemany" in by_mode:
            row_t, em_t = by_mode["row"]["elapsed_s"], by_mode["executemany"]["elapsed_s"]
            if em_t:
                print(f"  speedup (executemany vs row): {row_t / em_t:.2f}x")
        return 0


if __name__ == "__main__":
    sys.exit(main())
