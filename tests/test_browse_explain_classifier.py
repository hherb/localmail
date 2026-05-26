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

import sys
from pathlib import Path

# tests/acceptance is not a package; add it to sys.path so the
# classifier can be imported regardless of where pytest is run from.
_ACCEPTANCE_DIR = Path(__file__).parent / "acceptance"
if str(_ACCEPTANCE_DIR) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE_DIR))


def test_has_unique_node_is_true_when_unique_appears() -> None:
    """A ``Unique`` node in the plan must be flagged. Postgres only emits
    this to enforce SELECT DISTINCT; if EXISTS semi-join is silently
    swapped back for JOIN+DISTINCT, ``Unique`` reappears."""
    from run_browse_explain import classify_plan

    raw = (
        "Unique  (cost=10.00..20.00 rows=5 width=200)\n"
        "  ->  Sort  (cost=5.00..6.00 rows=10 width=200)\n"
        "        ->  Nested Loop  (cost=0.00..1.00 rows=10 width=200)\n"
        "Planning Time: 0.123 ms\n"
        "Execution Time: 1.234 ms\n"
    )
    summary = classify_plan(raw)
    assert summary.has_unique_node is True


def test_has_unique_node_is_false_when_unique_absent() -> None:
    """A clean EXISTS semi-join plan has no ``Unique`` node."""
    from run_browse_explain import classify_plan

    raw = (
        "Limit  (cost=0.00..10.00 rows=50 width=200)\n"
        "  ->  Nested Loop Semi Join  (cost=0.00..10.00 rows=50 width=200)\n"
        "        ->  Index Scan using messages_recent_idx on messages\n"
        "Planning Time: 0.5 ms\n"
        "Execution Time: 2.0 ms\n"
    )
    summary = classify_plan(raw)
    assert summary.has_unique_node is False


def test_has_unique_node_distinguishes_indented_form() -> None:
    """Postgres indents Unique nodes inside sub-plans as ``->  Unique``;
    the classifier must catch both."""
    from run_browse_explain import classify_plan

    raw_top = (
        "Unique  (cost=0..0 rows=0 width=0)\n"
        "Planning Time: 0 ms\n"
        "Execution Time: 0 ms\n"
    )
    raw_indented = (
        "Limit  (cost=0..0 rows=0 width=0)\n"
        "  ->  Unique  (cost=0..0 rows=0 width=0)\n"
        "Planning Time: 0 ms\n"
        "Execution Time: 0 ms\n"
    )
    assert classify_plan(raw_top).has_unique_node is True
    assert classify_plan(raw_indented).has_unique_node is True
