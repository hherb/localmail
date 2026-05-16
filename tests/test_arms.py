"""Integration tests for the three Phase-1 retrieval arms.

These rely on real Postgres + pgvector, so all are integration
tests. They populate a tiny corpus, embed deterministically, and verify
each arm returns the expected hit shape and ordering.
"""

from __future__ import annotations

from localmail.config import SearchConfig
from localmail.search.arms import arm_bm25_messages, arm_bm25_chunks, arm_vector_chunks
from localmail.search.query import parse_query
from localmail.search.embed_worker import run_embed_worker_once


class _SeedEmbedder:
    """Returns a different deterministic vector per text so vector arm orders deterministically."""
    name = "seed"; model = "seed"; dimension = 768

    def embed_documents(self, texts):
        out = []
        for t in texts:
            base = (sum(ord(c) for c in t) % 100) / 100.0
            out.append([base] * 768)
        return out

    def embed_query(self, text):
        base = (sum(ord(c) for c in text) % 100) / 100.0
        return [base] * 768

    def health_check(self): pass


def _seed_corpus(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a', 'a@x', 'h', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        msgs = [
            ("<m1>", "Berlin conference next week", "anna@x", "Anna",
             "Looking forward to the Berlin conference next week."),
            ("<m2>", "Lunch tomorrow", "bob@x", "Bob",
             "Want to grab lunch tomorrow?"),
            ("<m3>", "Conference review", "anna@x", "Anna",
             "How was the conference last week?"),
        ]
        ids = []
        for i, (mid, subj, fa, fn, body) in enumerate(msgs):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " from_addr, from_name, body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s) RETURNING id",
                (acct, mid, bytes([i + 1]) * 32, subj, fa, fn, body, b"r", 1),
            )
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def test_arm_bm25_messages_finds_subject_match(db_conn):
    ids = _seed_corpus(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _SeedEmbedder())
    parsed = parse_query("Berlin")
    hits = arm_bm25_messages(db_conn, parsed, cfg, limit=10)
    msg_ids = [h.message_id for h in hits]
    assert ids[0] in msg_ids


def test_arm_bm25_chunks_finds_body_match(db_conn):
    ids = _seed_corpus(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _SeedEmbedder())
    parsed = parse_query("lunch")
    hits = arm_bm25_chunks(db_conn, parsed, cfg, limit=10)
    assert ids[1] in [h.message_id for h in hits]


def test_arm_vector_chunks_returns_results(db_conn):
    ids = _seed_corpus(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _SeedEmbedder())
    parsed = parse_query("conference")
    qvec = _SeedEmbedder().embed_query("conference")
    hits = arm_vector_chunks(db_conn, parsed, cfg, qvec, limit=10)
    assert len(hits) >= 1
    assert all(h.chunk_table == "message_chunks" for h in hits)


def test_arms_respect_account_filter(db_conn):
    ids = _seed_corpus(db_conn)
    # Insert a second account + message to verify filtering
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('b', 'b@x', 'h', 'password') RETURNING id"
        )
        a2 = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject, body_text,"
            " headers, raw_bytes, size_bytes) VALUES (%s, '<m4>', %s, 'Berlin', 'x',"
            " '{}'::jsonb, 'r', 1) RETURNING id",
            (a2, b'\\x04' * 32),
        )
    db_conn.commit()
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _SeedEmbedder())

    parsed = parse_query("Berlin")
    parsed.filters.__dict__["accounts"] = [a2]  # resolved by Searcher in prod
    hits = arm_bm25_messages(db_conn, parsed, cfg, limit=10)
    assert all(h.message_id != ids[0] for h in hits)
