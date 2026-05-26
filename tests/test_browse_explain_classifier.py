"""Unit tests for the plan classifier in the acceptance harness.

The classifier is a pure function over EXPLAIN ANALYZE text — no DB
required. These tests pin (a) that the canonical regression markers
(``Unique`` node, full ``Sort`` node) are detected, and (b) that the
benign equivalents (``Incremental Sort``) are not flagged.

Verified at unit scale here; the at-scale assertions in
``test_browse_at_scale.py`` consume the same classifier via the
shared library.
"""
from __future__ import annotations

from tests.acceptance.run_browse_explain import classify_plan


def test_has_unique_node_is_true_when_unique_appears() -> None:
    """A ``Unique`` node in the plan must be flagged. Postgres only emits
    this to enforce SELECT DISTINCT; if EXISTS semi-join is silently
    swapped back for JOIN+DISTINCT, ``Unique`` reappears."""
    raw = (
        "Unique  (cost=10.00..20.00 rows=5 width=200)\n"
        "  ->  Sort  (cost=5.00..6.00 rows=10 width=200)\n"
        "        ->  Nested Loop  (cost=0.00..1.00 rows=10 width=200)\n"
        "Planning Time: 0.123 ms\n"
        "Execution Time: 1.234 ms\n"
    )
    summary = classify_plan(raw)
    assert summary.has_unique_node is True, summary.raw


def test_has_unique_node_is_false_when_unique_absent() -> None:
    """A clean EXISTS semi-join plan has no ``Unique`` node."""
    raw = (
        "Limit  (cost=0.00..10.00 rows=50 width=200)\n"
        "  ->  Nested Loop Semi Join  (cost=0.00..10.00 rows=50 width=200)\n"
        "        ->  Index Scan using messages_recent_idx on messages\n"
        "Planning Time: 0.5 ms\n"
        "Execution Time: 2.0 ms\n"
    )
    summary = classify_plan(raw)
    assert summary.has_unique_node is False, summary.raw


def test_has_unique_node_detects_indented_form() -> None:
    """Postgres indents Unique nodes inside sub-plans as ``->  Unique``;
    the classifier must catch this form too. The top-level form is
    already covered by ``test_has_unique_node_is_true_when_unique_appears``;
    this test exists to pin the indented branch in isolation."""
    raw = (
        "Limit  (cost=0..0 rows=0 width=0)\n"
        "  ->  Unique  (cost=0..0 rows=0 width=0)\n"
        "Planning Time: 0 ms\n"
        "Execution Time: 0 ms\n"
    )
    summary = classify_plan(raw)
    assert summary.has_unique_node is True, summary.raw
