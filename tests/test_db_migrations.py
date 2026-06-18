# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the migration runner's @non-transactional detection."""

from __future__ import annotations

from localmail.db import _is_non_transactional, _split_statements


def test_non_transactional_header_detected() -> None:
    sql = "-- @non-transactional\nCREATE INDEX CONCURRENTLY foo_idx ON foo (x);\n"
    assert _is_non_transactional(sql) is True


def test_no_header_means_transactional() -> None:
    sql = "-- ordinary migration\nCREATE TABLE foo (id int);\n"
    assert _is_non_transactional(sql) is False


def test_header_must_be_in_leading_comment_block() -> None:
    """A @non-transactional marker buried mid-file is ignored — too risky."""
    sql = "CREATE TABLE foo (id int);\n-- @non-transactional\nCREATE INDEX bar ON foo (id);\n"
    assert _is_non_transactional(sql) is False


def test_split_statements_simple() -> None:
    sql = "CREATE TABLE a (id int);\nCREATE INDEX a_id ON a (id);\n"
    out = _split_statements(sql)
    assert len(out) == 2
    assert out[0].startswith("CREATE TABLE")
    assert out[1].startswith("CREATE INDEX")


def test_split_statements_drops_pure_comment_fragments() -> None:
    """A standalone -- comment after the last `;` is dropped, but a leading
    comment attached to a real statement stays with that statement."""
    sql = "-- leading\nCREATE TABLE a (id int);\n-- trailing only\n"
    out = _split_statements(sql)
    assert len(out) == 1
    assert "CREATE TABLE a (id int)" in out[0]


def test_split_statements_preserves_semicolon_inside_string_literal() -> None:
    """A `;` inside `'...'` must NOT split the statement."""
    sql = "INSERT INTO t (msg) VALUES ('hello; world');\nSELECT 1;\n"
    out = _split_statements(sql)
    assert len(out) == 2
    assert "hello; world" in out[0]


def test_split_statements_preserves_semicolon_inside_dollar_quote() -> None:
    """A `;` inside `$$ ... $$` must NOT split the statement."""
    sql = (
        "CREATE OR REPLACE FUNCTION f(a TEXT) RETURNS TEXT\n"
        "    LANGUAGE SQL IMMUTABLE\n"
        "    AS $$ SELECT a; $$;\n"
        "SELECT f('x');\n"
    )
    out = _split_statements(sql)
    assert len(out) == 2
    assert "SELECT a;" in out[0]
    assert out[1].strip().startswith("SELECT f")


def test_split_statements_preserves_semicolon_inside_tagged_dollar_quote() -> None:
    """A `;` inside `$tag$ ... $tag$` must NOT split the statement."""
    sql = (
        "DO $body$ BEGIN PERFORM 1; PERFORM 2; END $body$;\n"
        "SELECT 1;\n"
    )
    out = _split_statements(sql)
    assert len(out) == 2
    assert "PERFORM 1;" in out[0]
    assert "PERFORM 2;" in out[0]


def test_split_statements_migration_0006_shape() -> None:
    """The dollar-quoted IMMUTABLE function from 0006_search_indexes.sql
    must round-trip as a single statement."""
    sql = (
        "CREATE OR REPLACE FUNCTION localmail_arr_to_text(TEXT[]) RETURNS TEXT\n"
        "    LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE\n"
        "    AS $$ SELECT array_to_string($1, ' ') $$;\n"
        "DROP INDEX IF EXISTS messages_fts_idx;\n"
    )
    out = _split_statements(sql)
    assert len(out) == 2
    assert "localmail_arr_to_text" in out[0]
    assert out[1] == "DROP INDEX IF EXISTS messages_fts_idx"
