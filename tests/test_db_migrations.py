"""Tests for the migration runner's @non-transactional detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from localmail.db import _is_non_transactional, apply_migrations


def test_non_transactional_header_detected(tmp_path: Path) -> None:
    sql = "-- @non-transactional\nCREATE INDEX CONCURRENTLY foo_idx ON foo (x);\n"
    assert _is_non_transactional(sql) is True


def test_no_header_means_transactional(tmp_path: Path) -> None:
    sql = "-- ordinary migration\nCREATE TABLE foo (id int);\n"
    assert _is_non_transactional(sql) is False


def test_header_must_be_in_leading_comment_block(tmp_path: Path) -> None:
    """A @non-transactional marker buried mid-file is ignored — too risky."""
    sql = "CREATE TABLE foo (id int);\n-- @non-transactional\nCREATE INDEX bar ON foo (id);\n"
    assert _is_non_transactional(sql) is False
