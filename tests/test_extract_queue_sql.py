# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for `search.extract_queue`'s SQL and the type that reads it — the
fragments, the parameters they bind, the partition they derive, the plan they
produce, and what `QueueCounts` refuses to represent.

`test_extract_queue.py` is the companion: it asks what the queue counts say
about a seeded archive. This file asks what the statements themselves are made
of, which is where two defects live that no count can reveal.

**#280 — the eligibility predicate must not correlate on the blob.** The
extension half reads original filenames out of `messages.attachments` (#216).
Written as a correlated ``EXISTS`` it was a `SubPlan` re-executed once per
blob, and Postgres abandons ``messages_attachments_gin`` the moment the operand
stops being a constant: cost ~36,203 per execution instead of ~42, measured at
**13:04** for one counter on a 127k-message archive. Nothing about the
*answers* changes when that is fixed, so only a plan assertion can pin it.

**#284 — the four buckets must partition the eligible population.** The sum
check `QueueCounts.__post_init__` shipped with is implied by the partition but
does not imply it: one blob counted twice plus another counted not at all sums
correctly. `misfiled_count_sql` closes that, and is tested here against
contrived bucket sets because the real predicates cannot be made to overlap
from data alone — which is precisely why the guard is for a future *predicate*
edit rather than for a future row.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import fields

import psycopg
import pytest

from localmail.search import extract_queue

# --------------------------------------------------------------------------
# The fragments and their parameters
# --------------------------------------------------------------------------


def test_every_placeholder_in_the_sql_has_a_parameter_helper_key() -> None:
    """A renamed placeholder must break here, not at runtime.

    The fragments are strings, so a rename in one and not the other is
    invisible until psycopg raises `ProgrammingError` on a real archive.
    """
    referenced = set(re.findall(r"%\((\w+)\)s", extract_queue.QUEUE_COUNTS_SQL))
    supplied = set(
        extract_queue.cap_params(max_retries=1, max_transient_retries=2)
    ) | set(
        extract_queue.allowlist_params(
            mime_allowlist=["text/plain"], extension_allowlist=[".txt"]
        )
    )
    assert referenced == supplied


def test_claim_fragments_reference_only_the_cap_parameters() -> None:
    """The claim has no allowlist half — it is applied in Python (#216)."""
    referenced = set(
        re.findall(
            r"%\((\w+)\)s",
            extract_queue.QUEUE_FROM_SQL + extract_queue.CLAIMABLE_WHERE_SQL,
        )
    )
    assert referenced == set(
        extract_queue.cap_params(max_retries=1, max_transient_retries=2)
    )


def test_the_claim_join_shape_never_touches_messages() -> None:
    """`QUEUE_FROM_SQL` is shared with `_claim_batch`, which runs per sweep
    under ``FOR UPDATE … SKIP LOCKED``. The extension lookup belongs to the
    report alone, so it hangs off `QUEUE_COUNTS_FROM_SQL` instead — putting it
    here would put a scan of every message on the worker's hot path.
    """
    assert "messages" not in extract_queue.QUEUE_FROM_SQL
    assert "messages" in extract_queue.QUEUE_COUNTS_FROM_SQL


def test_the_claimable_total_query_has_no_allowlist_half() -> None:
    """`claimable` is the worker's true queue depth, so it must not inherit the
    report's allowlist scoping — that gap is the whole reason it exists."""
    referenced = set(re.findall(r"%\((\w+)\)s", extract_queue.CLAIMABLE_TOTAL_SQL))
    assert referenced == set(
        extract_queue.cap_params(max_retries=1, max_transient_retries=2)
    )
    assert "jsonb_array_elements" not in extract_queue.CLAIMABLE_TOTAL_SQL


def test_allowlist_params_lowercases_both_allowlists() -> None:
    """`attachment_kind.is_allowlisted` lowers the configured values as well as
    the stored ones; the SQL assumes this half has already been done."""
    params = extract_queue.allowlist_params(
        mime_allowlist=["Application/PDF"], extension_allowlist=[".PDF"]
    )
    assert params == {
        "mime_allowlist": ["application/pdf"],
        "extension_allowlist": [".pdf"],
    }


# --------------------------------------------------------------------------
# One authority for what the buckets are
# --------------------------------------------------------------------------


def test_the_bucket_names_are_queue_counts_fields() -> None:
    """`BUCKET_WHERE_SQL` drives the SELECT's aliases, the misfiled expression
    *and* the sum check, so a bucket whose name is not a field would fail at
    `class_row` construction on a real archive rather than here."""
    assert set(extract_queue.BUCKET_WHERE_SQL) <= {
        f.name for f in fields(extract_queue.QueueCounts)
    }
    assert set(extract_queue.BUCKET_WHERE_SQL) == {
        "extracted",
        "no_text",
        "gave_up",
        "pending",
    }


def test_the_counts_query_derives_one_aggregate_per_bucket() -> None:
    """Typed out by hand, the SELECT and the partition check drifted apart the
    moment either grew a bucket; derived, they cannot."""
    for name in extract_queue.BUCKET_WHERE_SQL:
        assert f"AS {name}" in extract_queue.QUEUE_COUNTS_SQL
    assert "AS misfiled" in extract_queue.QUEUE_COUNTS_SQL


def test_bucket_count_sql_aliases_each_predicate_by_its_bucket_name() -> None:
    sql = extract_queue.bucket_count_sql({"a": "TRUE", "b": "FALSE"})
    assert "count(*) FILTER (WHERE TRUE) AS a" in sql
    assert "count(*) FILTER (WHERE FALSE) AS b" in sql


# --------------------------------------------------------------------------
# #284: the misfiled aggregate actually detects a broken partition
# --------------------------------------------------------------------------


def _misfiled_over_one_row(conn: psycopg.Connection, buckets: Mapping[str, str]) -> int:
    """Run `misfiled_count_sql` over a single synthetic row.

    A ``VALUES`` source rather than a seeded archive because the production
    predicates cannot be made to overlap from data — the thing under test is
    the detector, not the schema.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {extract_queue.misfiled_count_sql(buckets)}"
            f" FROM (VALUES (1)) AS b(x)"
        )
        row = cur.fetchone()
        assert row is not None
    return row[0]


def test_a_row_in_exactly_one_bucket_is_not_misfiled(db_conn) -> None:
    assert _misfiled_over_one_row(db_conn, {"a": "TRUE", "b": "FALSE"}) == 0


def test_a_row_in_two_buckets_is_misfiled(db_conn) -> None:
    """Overlapping predicates. Caught by the sum too — but only until a second
    blob falls through every bucket and compensates, which is #284's point."""
    assert _misfiled_over_one_row(db_conn, {"a": "TRUE", "b": "TRUE"}) == 1


def test_a_row_in_no_bucket_is_misfiled(db_conn) -> None:
    assert _misfiled_over_one_row(db_conn, {"a": "FALSE", "b": "FALSE"}) == 1


def test_a_three_valued_predicate_is_misfiled(db_conn) -> None:
    """The scenario the guard is really for: a migration relaxing one of the
    ``NOT NULL`` columns the predicates pivot on
    (``attachment_text.extracted_text``, ``failed_extractions.retry_count``,
    ``transient_extractions.transient_count``). SQL ``NULL`` makes a row match
    neither a predicate nor its complement, and ``IS DISTINCT FROM`` is what
    keeps that from evaluating to ``NULL`` and being silently uncounted.
    """
    assert _misfiled_over_one_row(db_conn, {"a": "NULL::boolean", "b": "FALSE"}) == 1


def test_queue_counts_rejects_buckets_that_do_not_account_for_every_blob() -> None:
    """The four buckets partition the eligible set; a gap is a predicate bug.

    Reported rather than silently absorbed, because the number an operator
    reads is the whole point of the command.
    """
    with pytest.raises(extract_queue.QueueCountsInconsistent, match="do not sum"):
        extract_queue.QueueCounts(
            eligible=10, extracted=1, no_text=1, gave_up=1, pending=1
        )


def test_queue_counts_accepts_a_partition() -> None:
    counts = extract_queue.QueueCounts(
        eligible=10, extracted=4, no_text=3, gave_up=2, pending=1
    )
    assert counts.eligible == 10


def test_claimable_is_outside_the_partition_check() -> None:
    """It comes from a second statement, so a worker committing between the two
    can briefly put it below `pending`. Crashing over that race would be worse
    than reporting it."""
    counts = extract_queue.QueueCounts(
        eligible=1, extracted=0, no_text=0, gave_up=0, pending=1, claimable=0
    )
    assert counts.claimable == 0


def test_queue_counts_has_no_truth_value() -> None:
    """`if counts:` is the implicit read that caused #251 and #259."""
    counts = extract_queue.QueueCounts(
        eligible=0, extracted=0, no_text=0, gave_up=0, pending=0
    )
    with pytest.raises(TypeError, match="no truth value"):
        bool(counts)


def test_status_field_names_covers_every_reported_field() -> None:
    """The CLI projects these onto its payload, so a bucket added to the type
    must not be able to go missing from the command that reports it."""
    counts = extract_queue.QueueCounts(
        eligible=3, extracted=1, no_text=1, gave_up=1, pending=0, claimable=9
    )
    assert set(counts.status_fields()) == set(
        extract_queue.QueueCounts.status_field_names()
    )
    assert counts.status_fields()["blobs_claimable"] == 9
    assert counts.status_fields()["blobs_eligible"] == 3


def test_queue_counts_rejects_a_misfiled_row_even_when_the_buckets_sum() -> None:
    """The gap the sum check leaves: one blob double-counted, another dropped.

    Checked before the sum so the message names the specific breakage —
    a misfiled row usually disturbs the sum too, and "does not sum" would
    send the reader looking for a missing bucket instead of an overlapping
    predicate.
    """
    with pytest.raises(extract_queue.QueueCountsInconsistent, match="misfiled"):
        extract_queue.QueueCounts(
            eligible=10, extracted=4, no_text=3, gave_up=2, pending=1, misfiled=2
        )


def test_misfiled_is_not_reported_to_the_operator() -> None:
    """It can only ever be zero on an instance that exists — `__post_init__`
    raises otherwise — so a permanently-zero line in `search-status` would be
    noise inviting exactly the wrong question."""
    names = extract_queue.QueueCounts.status_field_names()
    assert "blobs_misfiled" not in names
    assert "blobs_eligible" in names
    assert "blobs_claimable" in names


# --------------------------------------------------------------------------
# #280: the plan
# --------------------------------------------------------------------------

# The pre-#280 eligibility predicate, kept verbatim so the plan assertion above
# can be shown to have teeth — the same role `--predicate-form pre75` plays in
# `tests/acceptance/run_browse_explain.py`. Do not "fix" it; it is a museum
# piece.
_PRE280_CORRELATED_ALLOWLIST_SQL = """
    lower(b.mime_type) = ANY(%(mime_allowlist)s)
    OR EXISTS (
        SELECT 1
          FROM messages m, jsonb_array_elements(m.attachments) AS a
         WHERE m.attachments @> jsonb_build_array(
                 jsonb_build_object('sha256', encode(b.sha256,'hex')))
           AND a->>'sha256' = encode(b.sha256,'hex')
           AND lower(substring(a->>'filename' FROM '.(\\.[^.]+)$'))
               = ANY(%(extension_allowlist)s))
"""

_PRE280_COUNTS_SQL = f"""
    SELECT count(*) AS eligible
    {extract_queue.QUEUE_FROM_SQL}
    WHERE ({_PRE280_CORRELATED_ALLOWLIST_SQL})
"""

_PROBE_PARAMS: dict[str, object] = {
    **extract_queue.cap_params(max_retries=3, max_transient_retries=5),
    **extract_queue.allowlist_params(
        mime_allowlist=["application/pdf"], extension_allowlist=[".pdf"]
    ),
}


def _plan_root(conn: psycopg.Connection, sql: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + sql, _PROBE_PARAMS)
        row = cur.fetchone()
        assert row is not None
    return row[0][0]["Plan"]


def _scans_inside_a_subplan(
    node: dict, relation: str, *, inside: bool = False
) -> list[str]:
    """Node types scanning `relation` from underneath a ``SubPlan``.

    A base-relation scan lands there only when a subquery references the outer
    row, and Postgres then re-executes it once per outer row. That is the whole
    of #280, and it is a property of the plan tree rather than of the timings —
    so it holds at fixture scale, where the tables are far too small for a
    wall-clock assertion to mean anything.
    """
    hits = (
        [node["Node Type"]]
        if inside and node.get("Relation Name") == relation
        else []
    )
    for child in node.get("Plans", []):
        hits += _scans_inside_a_subplan(
            child,
            relation,
            inside=inside or child.get("Parent Relationship") == "SubPlan",
        )
    return hits


def test_the_eligibility_query_reads_messages_outside_any_subplan(db_conn) -> None:
    """#280's acceptance, as a plan property: one pass over `messages`, not one
    per blob."""
    plan = _plan_root(db_conn, extract_queue.QUEUE_COUNTS_SQL)
    assert _scans_inside_a_subplan(plan, "messages") == []


def test_the_pre_fix_predicate_reads_messages_inside_a_subplan(db_conn) -> None:
    """The assertion above passes trivially against a query that never mentions
    `messages` at all, so this pins that it can fail — the correlated form the
    fix replaced puts the scan exactly where the walk looks."""
    plan = _plan_root(db_conn, _PRE280_COUNTS_SQL)
    assert _scans_inside_a_subplan(plan, "messages") != []


def test_the_claim_query_never_reads_messages_at_all(db_conn) -> None:
    """The worker's own claim must stay a three-way primary-key join; #280's
    cost lives entirely in the report."""
    plan = _plan_root(db_conn, extract_queue.CLAIMABLE_TOTAL_SQL)
    assert "messages" not in str(plan)
