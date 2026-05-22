"""Measure the planner choice for ``localmail.api.browse.list_messages``.

Issue #72: the ``messages_recent_idx`` expression index on ``messages``
covers ``COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC``
but does **not** include ``account_id``. The per-user ACL filter
adds ``m.account_id = ANY(%s)`` on every browse query, so the planner
has two options:

1. **Index walk** on ``messages_recent_idx``, recheck ``account_id``
   per tuple. Streams in date order, short-circuits at ``LIMIT``.
2. **Bitmap heap scan** on the per-account index +
   ``Sort`` + ``Limit``. Materialises every matching row, ignores
   the date-ordered index. Catastrophic for keyset pagination
   when the ACL admits most of the table.

This harness builds a realistic-shape synthetic archive, runs
``EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT TEXT)`` against the
exact SQL emitted by ``list_messages`` for a matrix of ACL widths and
keyset positions, and prints which plan family fires. Use it to decide
whether to ship a covering index migration.

Usage::

    LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test \\
      PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \\
      --total-rows 100000 --accounts 5

Optional flags:

* ``--distribution {balanced,skewed,tail}`` (default ``skewed``)
* ``--page-size N`` (default 50, matches the GUI's default page size)
* ``--predicate-form {current,pre75}`` — choose the mid-keyset cursor
  predicate. ``current`` (default) is what ``api/browse.py:list_messages``
  emits after #75. ``pre75`` is the buggy form with the
  ``OR COALESCE IS NULL`` disjunct, kept here so the operator can
  reproduce the before/after difference on demand.
* ``--keep-data`` — leave the seeded rows in place (useful for ad-hoc
  ``psql`` follow-up)
* ``--json`` — emit machine-readable summary instead of the table

Prerequisites: a reachable Postgres at ``LOCALMAIL_TEST_DSN``, schema
at migration 0019 or later. The script TRUNCATEs the listed tables
before seeding — never run against a live archive.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from localmail.api.browse import (
    BROWSE_ROW_SQL_TEMPLATE, build_where, compose_browse_sql,
)
from localmail.api.browse_cursor import BrowseCursor
from localmail.db import apply_migrations


# ---- Seed parameters ----------------------------------------------------

# Anchor for synthesised ``internal_date`` / ``date_sent`` values. Stable
# across runs so the keyset cursor we mint is reproducible.
_EPOCH_ANCHOR = datetime(2024, 1, 1, tzinfo=timezone.utc)

# Fraction of rows where ``internal_date`` is NULL (legacy pre-0018 rows
# that haven't been backfilled). Set to a non-zero value because the
# planner's choice can hinge on the COALESCE branch ratio.
_NULL_INTERNAL_DATE_FRAC = 0.10

# Fraction of rows where both date columns are NULL (pathological
# headerless mail). These land in the NULLS-LAST tail.
_BOTH_NULL_FRAC = 0.01

# How wide a span of synthetic dates to spread the rows over. Wider
# spans push the index walk to read more index leaves before LIMIT;
# narrower spans make the per-account distribution matter more.
_DATE_SPAN_DAYS = 365 * 3

# Distribution presets — fraction of total rows that lands in each
# account, padded to ``num_accounts`` with the remainder split evenly.
_DISTRIBUTIONS: dict[str, list[float]] = {
    "balanced": [],  # uniform split, computed at run time
    "skewed":   [0.85],  # one account dominates
    "tail":     [0.01, 0.04, 0.05],  # several thin tails + everything else
}

# Page sizes to probe. The GUI requests 50; we add 5 to validate that
# the result is page-size-insensitive (it should be — short-circuit at
# LIMIT depends only on the chosen access path).
_DEFAULT_PAGE_SIZE = 50

# Truncate scope mirrors the other acceptance harnesses — this is the
# union of all data tables this script could touch, ordered so CASCADE
# isn't strictly necessary but is kept for safety.
_TRUNCATE_SQL = (
    "TRUNCATE accounts, mailboxes, messages, message_labels,"
    " attachment_blobs, failed_messages, message_chunks,"
    " failed_embeddings, embedding_models, failed_chunkings,"
    " attachment_text, attachment_chunks, failed_extractions,"
    " api_users, api_tokens, user_accounts, api_login_attempts"
    " RESTART IDENTITY CASCADE"
)

# Insert batch size for COPY. Postgres handles much larger batches but
# 5000 keeps memory low and lets us print progress.
_COPY_BATCH = 5000


# ---- The exact query under test -----------------------------------------
#
# The ``current`` variants below compose the production
# ``BROWSE_ROW_SQL_TEMPLATE`` (in ``localmail.api.browse``) via
# ``compose_browse_sql`` + ``build_where``. There is no duplicate SQL
# inline here — any refactor of the SELECT / FROM / ORDER BY or of the
# WHERE-clause emitter automatically lands in this harness (#77).
#
# Initial-load path: no ``folder_ids`` from the GUI today; the JOIN to
# ``message_labels`` is therefore skipped. DISTINCT remains for plan
# parity even though it is a no-op without the JOIN (the planner still
# has to consider it).
_INITIAL_PAGE_SQL = compose_browse_sql(
    folder_filter=False,
    where=build_where(
        # Placeholder account_ids — only the WHERE-clause TEXT matters
        # for composing the SQL string; actual values are bound per
        # probe via ``params`` in ``_run_explain``.
        account_ids=[0], folder_ids=None, cursor=None,
    )[0],
)

# Post-#75: the dated-cursor predicate uses SQL row comparison so
# Postgres composes it as a single Index Cond on the
# ``messages_recent_idx`` expression — a range-bounded scan that
# starts AT the cursor and only emits matching rows. NULL-tail rows
# are reached via a separate top-up query in
# ``api/browse.py:list_messages``, not by widening this clause.
#
# Parameter binding takes two cursor values (ts, id) — not three —
# unlike the OR form. Update ``--predicate-form`` plumbing if a new
# variant is added.
_MID_KEYSET_SQL = compose_browse_sql(
    folder_filter=False,
    where=build_where(
        account_ids=[0], folder_ids=None,
        cursor=BrowseCursor(
            # Placeholder cursor values for the same reason as above.
            ts=datetime(2024, 1, 1, tzinfo=timezone.utc), id=0,
        ),
    )[0],
)

# Pre-#75 baseline kept for ad-hoc before/after comparison. Selected
# via ``--predicate-form pre75``. This is intentionally NOT composed
# from the production primitives — the whole point is to reproduce
# the BUGGY shape so the operator can measure the perf delta on
# demand. Both the ``OR COALESCE IS NULL`` disjunct AND the OR-form
# keyset are present here — the disjunct was the original bug, and
# switching to ROW comparison was what actually composed the
# predicate as an Index Cond. ``Rows Removed by Filter`` at
# mid-keyset is ~total/2 on this form.
#
# The SELECT / FROM / ORDER BY shape DOES reuse
# ``BROWSE_ROW_SQL_TEMPLATE`` so the before/after comparison is
# strictly apples-to-apples — only the WHERE clause differs.
_PRE75_BUGGY_WHERE = (
    "m.account_id = ANY(%s)"
    " AND (COALESCE(m.internal_date, m.date_sent) < %s"
    "      OR (COALESCE(m.internal_date, m.date_sent) = %s AND m.id < %s)"
    "      OR COALESCE(m.internal_date, m.date_sent) IS NULL)"
)
_MID_KEYSET_SQL_PRE75 = BROWSE_ROW_SQL_TEMPLATE.format(
    join="", where=_PRE75_BUGGY_WHERE,
)


# ---- Plan classifier ----------------------------------------------------

@dataclass(frozen=True)
class PlanSummary:
    """Compact representation of an EXPLAIN ANALYZE result."""

    plan_family: str
    used_recent_idx: bool
    used_account_idx: bool
    has_full_sort: bool
    has_incremental_sort: bool
    rows_removed_by_filter: int
    actual_rows: int
    execution_ms: float
    planning_ms: float
    shared_hit_blocks: int
    shared_read_blocks: int
    raw: str = field(repr=False)


def classify_plan(explain_text: str) -> PlanSummary:
    """Pick the plan family out of an EXPLAIN ANALYZE textual output.

    The classifier is line-oriented and tolerant: it looks for the
    canonical node-name markers Postgres emits (``Index Scan using …``,
    ``Bitmap Heap Scan on``, ``Seq Scan on``, ``Sort``). The result
    drives the ``"would option 2 fire?"`` verdict at the bottom of
    the report.

    Distinction matters: ``Incremental Sort`` on top of an index walk
    is the cheap DISTINCT tie-breaker layered on a presorted stream —
    not a full sort over a materialised intermediate. Only a full
    ``Sort`` node (i.e. lacking the "Incremental" prefix) signals
    that the planner abandoned the date-ordered index.
    """
    lines = explain_text.splitlines()
    used_recent_idx = any("messages_recent_idx" in ln for ln in lines)
    used_account_idx = any(
        ("messages_acct_date_idx" in ln) or ("messages_acct_msgid_uniq" in ln)
        for ln in lines
    )
    has_incremental_sort = any("Incremental Sort" in ln for ln in lines)
    # A full Sort node is `->  Sort` *not* preceded by "Incremental".
    has_full_sort = any(
        (ln.strip().startswith("->  Sort") or ln.strip().startswith("Sort  "))
        and "Incremental Sort" not in ln
        for ln in lines
    )
    has_bitmap = any("Bitmap" in ln for ln in lines)
    has_seq_scan = any("Seq Scan on" in ln and "messages" in ln for ln in lines)

    if used_recent_idx and not has_full_sort:
        plan_family = "index-walk (option 1)"
    elif used_recent_idx and has_full_sort:
        plan_family = "index + full sort (degraded option 1)"
    elif has_bitmap:
        plan_family = "bitmap heap scan (option 2)"
    elif has_seq_scan:
        plan_family = "seq scan + sort (option 2, worst)"
    else:
        plan_family = "other"

    actual_rows = _scan_actual_rows(lines)
    rows_removed = _scan_rows_removed_on_messages(lines)
    execution_ms = _scan_timing(lines, "Execution Time:")
    planning_ms = _scan_timing(lines, "Planning Time:")
    shared_hit, shared_read = _scan_buffers(lines)
    return PlanSummary(
        plan_family=plan_family,
        used_recent_idx=used_recent_idx,
        used_account_idx=used_account_idx,
        has_full_sort=has_full_sort,
        has_incremental_sort=has_incremental_sort,
        rows_removed_by_filter=rows_removed,
        actual_rows=actual_rows,
        execution_ms=execution_ms,
        planning_ms=planning_ms,
        shared_hit_blocks=shared_hit,
        shared_read_blocks=shared_read,
        raw=explain_text,
    )


def _scan_actual_rows(lines: list[str]) -> int:
    """Pull the top-node ``actual rows=…`` count from an EXPLAIN output.

    Postgres ≥17 emits ``actual rows=51.00`` (fractional, loop-averaged);
    older versions emit an integer. Accept both.
    """
    for ln in lines:
        if "actual rows=" in ln:
            try:
                tail = ln.split("actual rows=", 1)[1]
                token = tail.split(" ", 1)[0]
                return int(float(token))
            except (IndexError, ValueError):
                continue
    return 0


def _scan_rows_removed_on_messages(lines: list[str]) -> int:
    """Sum ``Rows Removed by Filter:`` lines under the messages scan.

    A large value at the keyset-pagination probe is the signal that
    the cursor predicate's disjunction (``OR COALESCE IS NULL``)
    defeated the index range bound — the index walks every tuple
    above the cursor before the LIMIT short-circuits.
    """
    total = 0
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("Rows Removed by Filter:"):
            try:
                total += int(stripped.split(":", 1)[1].strip())
            except ValueError:
                continue
    return total


def _scan_timing(lines: list[str], label: str) -> float:
    """Pull a ``Planning Time`` / ``Execution Time`` value (in ms)."""
    for ln in lines:
        if ln.strip().startswith(label):
            try:
                return float(ln.split(label, 1)[1].strip().split(" ", 1)[0])
            except (IndexError, ValueError):
                continue
    return 0.0


def _scan_buffers(lines: list[str]) -> tuple[int, int]:
    """Sum top-level ``Buffers: shared hit=… read=…`` numbers."""
    shared_hit = 0
    shared_read = 0
    for ln in lines:
        stripped = ln.strip()
        if not stripped.startswith("Buffers:"):
            continue
        for token in stripped.split():
            if token.startswith("hit="):
                shared_hit += int(token.split("=", 1)[1].rstrip(","))
            elif token.startswith("read="):
                shared_read += int(token.split("=", 1)[1].rstrip(","))
    return shared_hit, shared_read


# ---- Seeding ------------------------------------------------------------

@dataclass(frozen=True)
class SeedConfig:
    total_rows: int
    num_accounts: int
    distribution: str
    null_internal_frac: float = _NULL_INTERNAL_DATE_FRAC
    both_null_frac: float = _BOTH_NULL_FRAC
    date_span_days: int = _DATE_SPAN_DAYS
    seed: int = 17


def _account_weights(num_accounts: int, distribution: str) -> list[float]:
    """Return per-account row-share weights, sum = 1.0.

    ``balanced`` → uniform split.  ``skewed`` → one account holds the
    fixed top fraction; the remainder splits evenly across the others.
    ``tail`` → several thin tails; the largest holds whatever is left.
    """
    if distribution not in _DISTRIBUTIONS:
        raise ValueError(f"unknown distribution: {distribution}")
    preset = _DISTRIBUTIONS[distribution]
    if not preset:
        share = 1.0 / num_accounts
        return [share] * num_accounts

    # The preset list contains the *small* weights; the remaining
    # accounts split (1 - sum(preset)) evenly.
    if len(preset) > num_accounts:
        raise ValueError(
            f"distribution {distribution!r} needs at least {len(preset)} accounts"
        )
    weights = list(preset)
    remainder = 1.0 - sum(preset)
    leftovers = num_accounts - len(preset)
    if leftovers > 0:
        weights.extend([remainder / leftovers] * leftovers)
    else:
        # Trim the preset and renormalise so the weights still sum to 1.0.
        weights = preset[:num_accounts]
        total = sum(weights)
        weights = [w / total for w in weights]
    # Largest first so account_id 1 is always the "heavy" account in
    # skewed / tail layouts. Makes the ACL probes more readable.
    weights.sort(reverse=True)
    return weights


def _seed_accounts(conn: psycopg.Connection, num_accounts: int) -> list[int]:
    """Insert ``num_accounts`` accounts, return their ids."""
    ids: list[int] = []
    with conn.cursor() as cur:
        for i in range(num_accounts):
            cur.execute(
                "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                " VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
                (f"acct{i}", f"acct{i}@example.test"),
            )
            row = cur.fetchone(); assert row is not None
            ids.append(int(row[0]))
    conn.commit()
    return ids


def _seed_messages(
    conn: psycopg.Connection, account_ids: list[int], cfg: SeedConfig,
) -> None:
    """Bulk-insert messages with account/date shapes matching ``cfg``.

    Uses ``COPY messages FROM STDIN`` for speed — at 100k rows the
    naive ``execute`` round-trip takes minutes.
    """
    rng = random.Random(cfg.seed)
    weights = _account_weights(len(account_ids), cfg.distribution)
    # Per-account row counts, summing to total_rows.
    counts = [int(round(w * cfg.total_rows)) for w in weights]
    # Patch any rounding drift so the totals match exactly.
    drift = cfg.total_rows - sum(counts)
    counts[0] += drift

    print(f"  seeding {cfg.total_rows:,} rows across "
          f"{len(account_ids)} account(s): {counts}", flush=True)

    t0 = time.monotonic()
    with conn.cursor() as cur:
        copy_sql = (
            "COPY messages (account_id, message_id, raw_sha256, subject,"
            " from_addr, from_name, date_sent, internal_date, headers,"
            " attachments, raw_bytes, size_bytes)"
            " FROM STDIN (FORMAT BINARY)"
        )
        with cur.copy(copy_sql) as cp:
            cp.set_types([
                "bigint", "text", "bytea", "text", "text", "text",
                "timestamptz", "timestamptz", "jsonb", "jsonb",
                "bytea", "int4",
            ])
            row_idx = 0
            for acct_id, count in zip(account_ids, counts):
                for _ in range(count):
                    row_idx += 1
                    date_sent, internal_date = _synth_dates(rng, cfg)
                    cp.write_row((
                        acct_id,
                        f"<m{row_idx}@bench.local>",
                        _synth_sha256(row_idx),
                        f"subj-{row_idx}",
                        f"from{row_idx % 23}@example.com",
                        f"Sender {row_idx % 23}",
                        date_sent,
                        internal_date,
                        "{}",
                        "[]",
                        b"raw",
                        4,
                    ))
    conn.commit()
    print(f"  seeded in {time.monotonic() - t0:.1f}s; running ANALYZE…",
          flush=True)
    with conn.cursor() as cur:
        cur.execute("ANALYZE messages")
        cur.execute("ANALYZE accounts")
    conn.commit()


def _synth_dates(
    rng: random.Random, cfg: SeedConfig,
) -> tuple[datetime | None, datetime | None]:
    """Pick a synthetic (date_sent, internal_date) pair per row."""
    coin = rng.random()
    days = rng.uniform(0, cfg.date_span_days)
    ts = _EPOCH_ANCHOR + timedelta(days=days)
    if coin < cfg.both_null_frac:
        return None, None
    if coin < cfg.both_null_frac + cfg.null_internal_frac:
        return ts, None
    # Common case: internal_date populated; date_sent ±1 day skew to
    # exercise the COALESCE branch.
    skew = timedelta(hours=rng.uniform(-24, 24))
    return ts + skew, ts


def _synth_sha256(row_idx: int) -> bytes:
    """Synthesise a unique 32-byte sha256 from the row index."""
    # Spread the index across all 32 bytes so the resulting bytea isn't
    # all zeros — keeps the unique index happy and matches real-shape data.
    raw = row_idx.to_bytes(8, "big") + b"\x00" * 24
    return raw


# ---- The probes ---------------------------------------------------------

@dataclass(frozen=True)
class ProbeSpec:
    """One ACL × keyset position combination."""

    name: str
    account_ids: list[int]
    cursor: tuple[datetime, int] | None  # None → initial page


def _build_probes(
    conn: psycopg.Connection, account_ids: list[int], page_size: int,
) -> list[ProbeSpec]:
    """Build the probe matrix.

    Picks four ACL widths (heavy, light, half, all) crossed with two
    keyset positions (initial, mid).
    """
    if not account_ids:
        return []
    heavy = [account_ids[0]]
    light = [account_ids[-1]]
    half = account_ids[: max(1, len(account_ids) // 2)]
    everything = list(account_ids)

    cursor_pos = _pick_mid_cursor(conn)
    out: list[ProbeSpec] = []
    for label, acl in [
        ("ACL=1 heavy", heavy),
        ("ACL=1 light", light),
        ("ACL=half",    half),
        ("ACL=all",     everything),
    ]:
        out.append(ProbeSpec(f"{label} | initial", acl, None))
        out.append(ProbeSpec(f"{label} | mid",     acl, cursor_pos))
    return out


def _pick_mid_cursor(conn: psycopg.Connection) -> tuple[datetime, int]:
    """Pick a (ts, id) tuple at roughly the 50th percentile in the
    sort order so the mid-keyset probe lands deep in the relation."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(internal_date, date_sent), id"
            "  FROM messages"
            " WHERE COALESCE(internal_date, date_sent) IS NOT NULL"
            " ORDER BY COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC"
            " OFFSET (SELECT COUNT(*)/2 FROM messages)"
            " LIMIT 1"
        )
        row = cur.fetchone()
        assert row is not None
        return (row[0], int(row[1]))


_PREDICATE_FORMS: dict[str, str] = {
    "current": _MID_KEYSET_SQL,
    "pre75": _MID_KEYSET_SQL_PRE75,
}


def _run_explain(
    conn: psycopg.Connection, probe: ProbeSpec, page_size: int,
    *, predicate_form: str = "current",
) -> PlanSummary:
    """Run EXPLAIN (ANALYZE, BUFFERS, VERBOSE) and classify the plan.

    ``predicate_form`` selects the mid-keyset SQL:
    ``"current"`` — post-#75, no OR-IS-NULL disjunct (default).
    ``"pre75"`` — the buggy form, kept for ad-hoc before/after comparison.
    """
    if probe.cursor is None:
        sql = _INITIAL_PAGE_SQL
        params: list[Any] = [probe.account_ids, page_size + 1]
    else:
        ts, mid = probe.cursor
        try:
            sql = _PREDICATE_FORMS[predicate_form]
        except KeyError as exc:
            raise ValueError(
                f"unknown predicate_form: {predicate_form!r}; "
                f"choose from {sorted(_PREDICATE_FORMS)}"
            ) from exc
        # ``current`` uses ROW comparison (2 cursor params); ``pre75``
        # uses the OR-form (3 cursor params). Keep the parameter
        # arity in sync with each SQL string.
        if predicate_form == "current":
            params = [probe.account_ids, ts, mid, page_size + 1]
        else:
            params = [probe.account_ids, ts, ts, mid, page_size + 1]
    explain_sql = "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT TEXT) " + sql
    with conn.cursor() as cur:
        cur.execute(explain_sql, params)
        rows = cur.fetchall()
    explain_text = "\n".join(r[0] for r in rows)
    return classify_plan(explain_text)


# ---- Reporting ----------------------------------------------------------

def _render_table(
    probes: list[ProbeSpec], summaries: list[PlanSummary],
) -> str:
    """Render the per-probe summary as a fixed-width table."""
    rows = [
        ("probe", "plan family", "rows", "filtered",
         "exec ms", "plan ms", "buf hit", "buf read")
    ]
    for probe, summ in zip(probes, summaries):
        rows.append((
            probe.name,
            summ.plan_family,
            f"{summ.actual_rows}",
            f"{summ.rows_removed_by_filter}",
            f"{summ.execution_ms:7.2f}",
            f"{summ.planning_ms:6.2f}",
            f"{summ.shared_hit_blocks}",
            f"{summ.shared_read_blocks}",
        ))
    col_widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    lines = []
    for i, r in enumerate(rows):
        line = "  ".join(c.ljust(col_widths[j]) for j, c in enumerate(r))
        lines.append(line)
        if i == 0:
            lines.append("  ".join("-" * w for w in col_widths))
    return "\n".join(lines)


def _verdict(summaries: list[PlanSummary]) -> str:
    """One-line verdict: option 1 universally, option 2 leakage, etc."""
    families = {s.plan_family for s in summaries}
    if all("option 1" in f for f in families):
        return (
            "VERDICT: every probe used the messages_recent_idx index "
            "walk (option 1). No covering index needed at this dataset "
            "shape."
        )
    if any("option 2" in f for f in families):
        bad = [f for f in families if "option 2" in f]
        return (
            f"VERDICT: option 2 fires on at least one probe — observed: "
            f"{', '.join(sorted(bad))}. A covering index keyed on "
            f"(account_id, COALESCE(internal_date, date_sent) DESC NULLS LAST, "
            f"id DESC) should be considered."
        )
    return f"VERDICT: mixed plan families: {sorted(families)} — inspect raw output."


# ---- Entrypoint ---------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--total-rows", type=int, default=100_000)
    parser.add_argument("--accounts", type=int, default=5)
    parser.add_argument(
        "--distribution", choices=sorted(_DISTRIBUTIONS), default="skewed",
    )
    parser.add_argument("--page-size", type=int, default=_DEFAULT_PAGE_SIZE)
    parser.add_argument(
        "--predicate-form", choices=sorted(_PREDICATE_FORMS), default="current",
        help=(
            "Mid-keyset cursor predicate to probe. 'current' (default) is "
            "the post-#75 range-seekable form; 'pre75' is the buggy form "
            "with OR COALESCE IS NULL, kept for before/after measurement."
        ),
    )
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--dsn", default=os.environ.get(
            "LOCALMAIL_TEST_DSN",
            "postgresql://localmail:local%40%40mail@localhost:5532/localmail_test",
        ),
    )
    args = parser.parse_args()

    print(f"connecting to {args.dsn}")
    apply_migrations(args.dsn)
    with psycopg.connect(args.dsn) as conn:
        print("truncating data tables…")
        with conn.cursor() as cur:
            cur.execute(_TRUNCATE_SQL)
        conn.commit()

        cfg = SeedConfig(
            total_rows=args.total_rows,
            num_accounts=args.accounts,
            distribution=args.distribution,
        )
        account_ids = _seed_accounts(conn, args.accounts)
        _seed_messages(conn, account_ids, cfg)
        probes = _build_probes(conn, account_ids, args.page_size)
        if not probes:
            print("no probes generated (no accounts?)", file=sys.stderr)
            return 1

        summaries: list[PlanSummary] = []
        for probe in probes:
            summ = _run_explain(
                conn, probe, args.page_size,
                predicate_form=args.predicate_form,
            )
            summaries.append(summ)

        if args.json:
            print(json.dumps({
                "config": {
                    "total_rows": args.total_rows,
                    "accounts": args.accounts,
                    "distribution": args.distribution,
                    "page_size": args.page_size,
                    "predicate_form": args.predicate_form,
                },
                "probes": [
                    {
                        "name": p.name,
                        "acl_size": len(p.account_ids),
                        "keyset": p.cursor is not None,
                        "plan_family": s.plan_family,
                        "actual_rows": s.actual_rows,
                        "rows_removed_by_filter": s.rows_removed_by_filter,
                        "execution_ms": s.execution_ms,
                        "planning_ms": s.planning_ms,
                        "buf_hit": s.shared_hit_blocks,
                        "buf_read": s.shared_read_blocks,
                    }
                    for p, s in zip(probes, summaries)
                ],
                "verdict": _verdict(summaries),
            }, indent=2))
        else:
            print("\n" + _render_table(probes, summaries))
            print("\n" + _verdict(summaries))
            print("\nRaw EXPLAIN output per probe written to stderr below "
                  "for debugging:", file=sys.stderr)
            for probe, summ in zip(probes, summaries):
                print(f"\n=== {probe.name} ===", file=sys.stderr)
                print(summ.raw, file=sys.stderr)

        if not args.keep_data:
            print("\ntruncating to leave a clean DB…")
            with conn.cursor() as cur:
                cur.execute(_TRUNCATE_SQL)
            conn.commit()

    return 0


if __name__ == "__main__":
    sys.exit(main())
