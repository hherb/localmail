"""Integration tests for the three Phase-1 retrieval arms.

These rely on real Postgres + pgvector, so all are integration
tests. They populate a tiny corpus, embed deterministically, and verify
each arm returns the expected hit shape and ordering.
"""

from __future__ import annotations

from localmail.config import SearchConfig
from localmail.search.arms import arm_bm25_messages, arm_bm25_chunks, arm_vector_chunks, arm_vector_attachment_chunks, build_lexical_tsquery
from localmail.search.query import parse_query, ParsedQuery, SearchFilters
from localmail.search.embed_worker import run_embed_worker_once


def test_lexical_tsquery_identity_with_no_expansion():
    sql, params = build_lexical_tsquery("hello world", [])
    assert sql == "plainto_tsquery('simple', %s)"
    assert params == ["hello world"]


def test_lexical_tsquery_ors_expansion_terms():
    sql, params = build_lexical_tsquery("invoice", ["bill", "receipt"])
    assert sql == (
        "(plainto_tsquery('simple', %s) || plainto_tsquery('simple', %s)"
        " || plainto_tsquery('simple', %s))"
    )
    assert params == ["invoice", "bill", "receipt"]


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


def test_arms_respect_label_filter(db_conn):
    ids = _seed_corpus(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _SeedEmbedder())

    # Create two mailboxes and assign each message to exactly one
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT account_id FROM messages WHERE id = %s", (ids[0],)
        )
        acct_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'Work') RETURNING id",
            (acct_id,),
        )
        mbox_work = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'Personal') RETURNING id",
            (acct_id,),
        )
        mbox_personal = cur.fetchone()[0]
        # ids[0] → Work, ids[1] → Personal
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) VALUES (%s, %s, 1)",
            (ids[0], mbox_work),
        )
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) VALUES (%s, %s, 2)",
            (ids[1], mbox_personal),
        )
    db_conn.commit()

    # Searching "conference" with label:Work should return ids[0] but not ids[1]
    parsed = parse_query("conference label:Work")
    hits = arm_bm25_messages(db_conn, parsed, cfg, limit=10)
    msg_ids = [h.message_id for h in hits]
    assert ids[0] in msg_ids
    assert ids[1] not in msg_ids


def test_arm_vector_attachment_chunks_returns_message_ids(db_conn) -> None:
    """Insert an attachment_blob + message that references it via JSONB
    attachments, plus an attachment_chunks row with a known embedding.
    Arm 4 should return the message_id."""
    import hashlib
    import json

    sha = hashlib.sha256(b"blob xyz").digest()
    sha_hex = sha.hex()

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('a','e@x','h','password') RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        acct_id = row[0]

        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/somewhere/foo.pdf", "application/pdf", 1000),
        )

        attachments = json.dumps(
            [{"filename": "report.pdf", "sha256": sha_hex}]
        )
        cur.execute(
            "INSERT INTO messages "
            "(account_id, message_id, raw_sha256, subject, body_text, "
            " headers, raw_bytes, size_bytes, attachments) "
            "VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s::jsonb) "
            "RETURNING id",
            (acct_id, "<m1@x>", b"\x10" * 32, "FYI", "see attached",
             b"raw", 3, attachments),
        )
        row = cur.fetchone()
        assert row is not None
        msg_id = row[0]

        unit = [0.0] * 768
        unit[0] = 1.0
        cur.execute(
            "INSERT INTO attachment_chunks "
            "(sha256, chunk_idx, text, token_count, embedding_v1, embedded_at) "
            "VALUES (%s, 0, %s, 10, %s::halfvec, now())",
            (sha, "attachment chunk text", unit),
        )
    db_conn.commit()

    cfg = SearchConfig()
    parsed = ParsedQuery(free_text="anything", filters=SearchFilters())
    hits = arm_vector_attachment_chunks(
        db_conn, parsed, cfg, qvec=unit, limit=10
    )

    assert len(hits) >= 1
    assert hits[0].message_id == msg_id
    assert hits[0].chunk_table == "attachment_chunks"


def test_arm_vector_attachment_chunks_fanout_cap_honored(db_conn) -> None:
    """A blob attached to N messages fans out to at most arm4_fanout_cap rows."""
    import hashlib, json

    sha = hashlib.sha256(b"popular blob").digest()
    sha_hex = sha.hex()
    attachments = json.dumps([{"filename": "x.pdf", "sha256": sha_hex}])

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('b','e@y','h','password') RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        acct_id = row[0]
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/p", "application/pdf", 100),
        )
        for i in range(25):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, "
                "subject, body_text, headers, raw_bytes, size_bytes, attachments) "
                "VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s::jsonb)",
                (acct_id, f"<m{i}@y>", bytes([i + 50]) * 32, f"S{i}", "",
                 b"r", 1, attachments),
            )
        unit = [0.0] * 768
        unit[0] = 1.0
        cur.execute(
            "INSERT INTO attachment_chunks (sha256, chunk_idx, text, "
            "token_count, embedding_v1, embedded_at) "
            "VALUES (%s, 0, %s, 10, %s::halfvec, now())",
            (sha, "chunk", unit),
        )
    db_conn.commit()

    cfg = SearchConfig(arm4_fanout_cap=10)
    parsed = ParsedQuery(free_text="x", filters=SearchFilters())
    hits = arm_vector_attachment_chunks(
        db_conn, parsed, cfg, qvec=unit, limit=100
    )
    assert len(hits) <= 10


def test_arm_vector_attachment_chunks_no_chunks_returns_empty(db_conn) -> None:
    """No attachment_chunks rows in DB → empty result, no error."""
    cfg = SearchConfig()
    parsed = ParsedQuery(free_text="x", filters=SearchFilters())
    unit = [0.0] * 768
    hits = arm_vector_attachment_chunks(
        db_conn, parsed, cfg, qvec=unit, limit=10
    )
    assert hits == []


def _seed_one(conn, subject, body):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a', 'a@x', 'h', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s)",
            (acct, "<m1>", b"\x01" * 32, subject, body, b"r", 1),
        )
    conn.commit()


def test_expansion_term_retrieves_synonym_only_message(db_conn):
    _seed_one(db_conn, subject="receipt for lunch", body="thanks")
    cfg = SearchConfig()

    base = ParsedQuery(free_text="invoice")
    assert arm_bm25_messages(db_conn, base, cfg, limit=10) == []   # no match

    expanded = ParsedQuery(free_text="invoice", expansion_terms=["receipt"])
    hits = arm_bm25_messages(db_conn, expanded, cfg, limit=10)
    assert len(hits) == 1                                          # synonym hit
