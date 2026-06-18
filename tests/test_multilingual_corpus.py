# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Sanity tests for the multilingual fixture corpus."""

from __future__ import annotations

from tests._multilingual_corpus import build_corpus


def test_build_corpus_returns_messages_for_all_target_languages(db_conn):
    msgs = build_corpus(db_conn)
    langs = {m["lang"] for m in msgs}
    assert {"de", "en", "es", "ja", "no"}.issubset(langs)
    assert len(msgs) >= 50


def test_build_corpus_inserts_into_messages_table(db_conn):
    msgs = build_corpus(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM messages")
        assert cur.fetchone()[0] == len(msgs)
