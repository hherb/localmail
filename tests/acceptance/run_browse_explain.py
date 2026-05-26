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
* ``--folder-filter`` — also seed ``message_labels`` rows and add
  folder-filter probes (selective ~5% + broad ~50% mailbox per
  account). Answers #78: what plan family does Postgres pick when
  ``folder_ids`` is non-empty? Post-#85 the production folder-filter
  predicate is ``WHERE EXISTS (SELECT 1 FROM message_labels …)``
  (a semi-join, not a JOIN+DISTINCT). The harness composes via
  production primitives, so any future production refactor lands
  here automatically.
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

# Fractions of per-account rows labelled into the two folder-filter
# probe mailboxes (#78). ``selective`` mirrors a label-like folder
# (Receipts, Newsletters); ``broad`` mirrors Inbox/Sent under Gmail's
# "every message gets multiple labels" model. Labelled rows are
# picked in id order so the broad mailbox is a strict superset of
# the selective one — the same shape the live mailbox tree settles
# into once Gmail's auto-labels run.
_FOLDER_FRACTIONS: dict[str, float] = {
    "selective": 0.05,
    "broad":     0.50,
}

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
# The ``current`` variants are composed at probe time in
# ``_initial_page_sql_and_params`` / ``_mid_keyset_sql_and_params`` from
# the production ``BROWSE_ROW_SQL_TEMPLATE`` (in
# ``localmail.api.browse``) via ``compose_browse_sql`` + ``build_where``
# — using the REAL account_ids and cursor values, not placeholders. So
# the param-shape contract between the SQL string and the bound params
# is owned by ``build_where`` itself; this harness never duplicates it.
# Any refactor of the SELECT / FROM / ORDER BY or of the WHERE-clause
# emitter automatically lands here (#77).
#
# Initial-load path: no ``folder_ids`` from the GUI today; the JOIN to
# ``message_labels`` is therefore skipped. DISTINCT remains for plan
# parity even though it is a no-op without the JOIN (the planner still
# has to consider it).


def _initial_page_sql_and_params(
    account_ids: list[int], page_size: int,
    *, folder_ids: list[int] | None = None,
) -> tuple[str, list[Any]]:
    """Compose the initial-page probe SQL + params from production primitives.

    Returns ``(sql, params)`` ready for ``cur.execute``; the caller
    prepends ``EXPLAIN …`` to the SQL. ``page_size + 1`` is appended
    as the LIMIT value here so the bound-param shape is owned in one
    place. ``folder_ids`` activates the EXISTS folder-filter predicate
    inside ``build_where`` (#85).
    """
    where, params = build_where(
        account_ids=account_ids, folder_ids=folder_ids, cursor=None,
    )
    return (
        compose_browse_sql(where=where),
        params + [page_size + 1],
    )


# Post-#75: the dated-cursor predicate uses SQL row comparison so
# Postgres composes it as a single Index Cond on the
# ``messages_recent_idx`` expression — a range-bounded scan that
# starts AT the cursor and only emits matching rows. NULL-tail rows
# are reached via a separate top-up query in
# ``api/browse.py:list_messages``, not by widening this clause.
#
# Parameter binding takes two cursor values (ts, id) — not three —
# unlike the OR form. ``build_where`` owns the contract; this helper
# just delegates.
def _mid_keyset_sql_and_params(
    account_ids: list[int], ts: datetime, mid: int, page_size: int,
    *, folder_ids: list[int] | None = None,
) -> tuple[str, list[Any]]:
    """Compose the mid-keyset probe SQL + params from production primitives.

    Returns ``(sql, params)`` ready for ``cur.execute``; the caller
    prepends ``EXPLAIN …`` to the SQL. ``page_size + 1`` is appended
    as the LIMIT value here so the bound-param shape is owned in one
    place. ``folder_ids`` activates the EXISTS folder-filter predicate
    inside ``build_where`` (#85).
    """
    where, params = build_where(
        account_ids=account_ids, folder_ids=folder_ids,
        cursor=BrowseCursor(ts=ts, id=mid),
    )
    return (
        compose_browse_sql(where=where),
        params + [page_size + 1],
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
    where=_PRE75_BUGGY_WHERE,
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
    has_unique_node: bool
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
    has_unique_node = any(
        ln.strip().startswith("Unique  ") or ln.strip().startswith("->  Unique  ")
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
        has_unique_node=has_unique_node,
        rows_removed_by_filter=rows_removed,
        actual_rows=actual_rows,
        execution_ms=execution_ms,
        planning_ms=planning_ms,
        shared_hit_blocks=shared_hit,
        shared_read_blocks=shared_read,
        raw=explain_text,
    )


def _scan_actual_rows(lines: list[str]) -> int:
    """Pull the top-node ``rows=…`` count from an ``EXPLAIN ANALYZE``
    line's ``(actual time=... rows=N loops=M)`` group.

    Postgres ≥18 emits ``rows=N.NN`` (loop-averaged, fractional);
    Postgres ≤17 emits ``rows=N`` (integer). Same lexeme position in
    both, so split on ``rows=`` *after* the ``actual time=`` anchor
    to avoid picking up the planner-estimate ``rows=N`` that sits
    earlier in the same line inside the ``cost=…`` group.

    The pre-#79 version searched for the literal ``"actual rows="``
    substring — which Postgres has never emitted — and therefore
    always returned 0.
    """
    for ln in lines:
        anchor = ln.find("actual time=")
        if anchor == -1:
            continue
        tail = ln[anchor:]
        if "rows=" not in tail:
            continue
        token = tail.split("rows=", 1)[1].split(" ", 1)[0]
        try:
            return int(float(token))
        except ValueError:
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


def _seed_folder_filter_mailboxes(
    conn: psycopg.Connection, account_ids: list[int],
) -> FolderMailboxes:
    """Create one ``selective`` and one ``broad`` mailbox per account
    and label the requested fraction of each account's messages into
    each (#78).

    The labelling SQL picks rows in id order so the ``broad`` mailbox
    is a strict superset of the ``selective`` one for the same
    account — mirrors the realistic Gmail shape where Inbox and Sent
    both label the same threads. UIDs are minted via ``row_number()``
    so the ``(mailbox_id, uid)`` unique constraint is satisfied
    without per-message round-trips.
    """
    print(f"  seeding folder-filter mailboxes + labels for "
          f"{len(account_ids)} account(s)…", flush=True)
    selective: list[int] = []
    broad: list[int] = []
    t0 = time.monotonic()
    with conn.cursor() as cur:
        for aid in account_ids:
            for name, fraction in _FOLDER_FRACTIONS.items():
                cur.execute(
                    "INSERT INTO mailboxes (account_id, name, uidvalidity)"
                    " VALUES (%s, %s, 1) RETURNING id",
                    (aid, name),
                )
                row = cur.fetchone()
                assert row is not None
                mb_id = int(row[0])
                cur.execute(
                    "WITH ranked AS ("
                    "  SELECT id,"
                    "         row_number() OVER (ORDER BY id) AS rn,"
                    "         count(*) OVER () AS total"
                    "    FROM messages WHERE account_id = %s"
                    ")"
                    " INSERT INTO message_labels (message_id, mailbox_id, uid)"
                    "   SELECT id, %s, rn FROM ranked"
                    "    WHERE rn <= ceil(total * %s)",
                    (aid, mb_id, fraction),
                )
                target = selective if name == "selective" else broad
                target.append(mb_id)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("ANALYZE mailboxes")
        cur.execute("ANALYZE message_labels")
    conn.commit()
    print(f"  seeded folder-filter rows in {time.monotonic() - t0:.1f}s",
          flush=True)
    return FolderMailboxes(selective=selective, broad=broad)


# ---- The probes ---------------------------------------------------------

@dataclass(frozen=True)
class ProbeSpec:
    """One ACL × keyset position combination.

    ``folder_ids`` is the optional folder-filter dimension added for #78.
    ``None`` means the GUI's initial-load path (no ``message_labels``
    JOIN); a non-empty list switches the SQL composition to the
    ``folder_filter=True`` branch of ``compose_browse_sql``.
    """

    name: str
    account_ids: list[int]
    cursor: tuple[datetime, int] | None  # None → initial page
    folder_ids: list[int] | None = None  # None → no folder filter


@dataclass(frozen=True)
class FolderMailboxes:
    """Per-account mailbox ids for the two folder-filter probe shapes."""

    selective: list[int]  # one mailbox id per account, in account_ids order
    broad: list[int]      # one mailbox id per account, in account_ids order


def _build_probes(
    cfg: SeedConfig, account_ids: list[int], page_size: int,
    *, folders: FolderMailboxes | None = None,
) -> list[ProbeSpec]:
    """Build the probe matrix.

    Picks four ACL widths (heavy, light, half, all) crossed with two
    keyset positions (initial, mid). When ``folders`` is supplied,
    appends four folder-filter probes — selective (5% of one
    account), broad (50% of one account), broad mid-keyset, and a
    broad-across-accounts probe that crosses the ACL=all width with
    one broad mailbox per account.

    The mid-keyset cursor is derived from ``cfg`` rather than queried
    from the live ``messages`` table — see :func:`_mid_cursor_from_seed`
    and issue #79.
    """
    if not account_ids:
        return []
    heavy = [account_ids[0]]
    light = [account_ids[-1]]
    half = account_ids[: max(1, len(account_ids) // 2)]
    everything = list(account_ids)

    cursor_pos = _mid_cursor_from_seed(cfg)
    out: list[ProbeSpec] = []
    for label, acl in [
        ("ACL=1 heavy", heavy),
        ("ACL=1 light", light),
        ("ACL=half",    half),
        ("ACL=all",     everything),
    ]:
        out.append(ProbeSpec(f"{label} | initial", acl, None))
        out.append(ProbeSpec(f"{label} | mid",     acl, cursor_pos))

    if folders is not None:
        out.extend(_build_folder_filter_probes(account_ids, cursor_pos, folders))
    return out


def _build_folder_filter_probes(
    account_ids: list[int],
    cursor_pos: tuple[datetime, int],
    folders: FolderMailboxes,
) -> list[ProbeSpec]:
    """Folder-filter probes added when ``--folder-filter`` is set (#78).

    Four probes:
    1. ``ACL=1 heavy | initial | folder=selective`` — narrow folder
       (~5%) on the heavy account; small post-JOIN result set.
    2. ``ACL=1 heavy | initial | folder=broad`` — broad folder (~50%)
       on the heavy account; large post-JOIN result set.
    3. ``ACL=1 heavy | mid | folder=broad`` — same broad folder with
       the deep-keyset cursor; mirrors the mid-keyset probe from the
       folderless matrix.
    4. ``ACL=all | initial | folder=broad-across-accounts`` — one
       broad mailbox per account, exercises the ``ml.mailbox_id =
       ANY(%s)`` predicate against a multi-element list.
    """
    heavy = [account_ids[0]]
    everything = list(account_ids)
    selective_heavy = [folders.selective[0]]
    broad_heavy = [folders.broad[0]]
    broad_all = list(folders.broad)
    return [
        ProbeSpec(
            "ACL=1 heavy | initial | folder=selective",
            heavy, None, folder_ids=selective_heavy,
        ),
        ProbeSpec(
            "ACL=1 heavy | initial | folder=broad",
            heavy, None, folder_ids=broad_heavy,
        ),
        ProbeSpec(
            "ACL=1 heavy | mid | folder=broad",
            heavy, cursor_pos, folder_ids=broad_heavy,
        ),
        ProbeSpec(
            "ACL=all | initial | folder=broad-across-accounts",
            everything, None, folder_ids=broad_all,
        ),
    ]


def _mid_cursor_from_seed(cfg: SeedConfig) -> tuple[datetime, int]:
    """Derive a mid-keyset cursor directly from the seed config (#79).

    The synthetic seed places ``COALESCE(internal_date, date_sent)``
    uniformly across ``[_EPOCH_ANCHOR, _EPOCH_ANCHOR + date_span_days]``
    (modulo the ~1% NULL tail), so the 50th-percentile date is
    ``_EPOCH_ANCHOR + date_span_days/2`` days. The ``id`` is the
    secondary tie-breaker only — any value inside the dense BIGSERIAL
    range works; we pick ``total_rows // 2`` so the cursor stays near
    the middle of the relation.

    Replaces the previous OFFSET-based picker that scanned half the
    table per call. The harness's wall-clock is unchanged at the
    default 100k rows and substantially faster at 5M+.
    """
    mid_ts = _EPOCH_ANCHOR + timedelta(days=cfg.date_span_days / 2)
    mid_id = cfg.total_rows // 2
    return (mid_ts, mid_id)


_VALID_PREDICATE_FORMS = ("current", "pre75")


def _run_explain(
    conn: psycopg.Connection, probe: ProbeSpec, page_size: int,
    *, predicate_form: str = "current",
) -> PlanSummary:
    """Run EXPLAIN (ANALYZE, BUFFERS, VERBOSE) and classify the plan.

    ``predicate_form`` selects the mid-keyset SQL:
    ``"current"`` — post-#75, no OR-IS-NULL disjunct (default).
    ``"pre75"`` — the buggy form, kept for ad-hoc before/after comparison.

    Folder-filter probes use the production EXISTS semi-join shape (#85);
    no comparison flag is exposed because the harness composes via
    production primitives, so a future refactor lands here automatically.
    """
    if predicate_form not in _VALID_PREDICATE_FORMS:
        raise ValueError(
            f"unknown predicate_form: {predicate_form!r}; "
            f"choose from {_VALID_PREDICATE_FORMS}"
        )
    if probe.cursor is None:
        sql, params = _initial_page_sql_and_params(
            probe.account_ids, page_size, folder_ids=probe.folder_ids,
        )
    else:
        ts, mid = probe.cursor
        if predicate_form == "current":
            sql, params = _mid_keyset_sql_and_params(
                probe.account_ids, ts, mid, page_size,
                folder_ids=probe.folder_ids,
            )
        else:
            # The ``pre75`` baseline uses the OR-form (3 cursor params)
            # and is intentionally hand-bound — the whole point is to
            # reproduce the BUGGY shape, not compose it from the
            # production primitives. ``pre75`` does not exercise the
            # folder-filter dimension; the bug it reproduces is
            # orthogonal.
            if probe.folder_ids:
                raise ValueError(
                    "predicate_form='pre75' is not combined with "
                    "folder filtering — the pre-#75 bug is orthogonal"
                )
            sql = _MID_KEYSET_SQL_PRE75
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


def _verdict(
    probes: list[ProbeSpec], summaries: list[PlanSummary],
) -> str:
    """Multi-line verdict, split by folder-filter dimension.

    Folderless probes drive the covering-index recommendation
    (#72 question): if any folderless probe falls to option 2, a
    covering index on ``(account_id, COALESCE(...), id DESC)``
    would be worth shipping.

    Folder-filter probes (#78) are reported informationally — the
    planner's choice is selectivity-dependent (narrow folder may
    correctly start from ``message_labels``; broad folder benefits
    from the date-ordered walk), so a single "option N fires"
    summary is meaningful but not actionable on its own.
    """
    folderless = [
        (p, s) for p, s in zip(probes, summaries) if p.folder_ids is None
    ]
    folder_filter = [
        (p, s) for p, s in zip(probes, summaries) if p.folder_ids is not None
    ]
    lines: list[str] = []
    lines.append(_verdict_for_folderless(folderless))
    if folder_filter:
        lines.append(_verdict_for_folder_filter(folder_filter))
    return "\n".join(lines)


def _verdict_for_folderless(
    pairs: list[tuple[ProbeSpec, PlanSummary]],
) -> str:
    """Verdict for the folderless probe matrix — covering-index recommendation."""
    if not pairs:
        return "VERDICT (folderless): no probes."
    families = {s.plan_family for _, s in pairs}
    if all("option 1" in f for f in families):
        return (
            "VERDICT (folderless): every probe used the "
            "messages_recent_idx index walk (option 1). No covering "
            "index needed at this dataset shape."
        )
    if any("option 2" in f for f in families):
        bad = [f for f in families if "option 2" in f]
        return (
            f"VERDICT (folderless): option 2 fires on at least one "
            f"folderless probe — observed: {', '.join(sorted(bad))}. "
            f"A covering index keyed on (account_id, "
            f"COALESCE(internal_date, date_sent) DESC NULLS LAST, "
            f"id DESC) should be considered."
        )
    return (
        f"VERDICT (folderless): mixed plan families: "
        f"{sorted(families)} — inspect raw output."
    )


def _verdict_for_folder_filter(
    pairs: list[tuple[ProbeSpec, PlanSummary]],
) -> str:
    """Verdict for the folder-filter probe matrix — informational only.

    The planner's choice depends on the join selectivity: a narrow
    folder (~5%) may correctly start from ``message_labels`` and look
    up matching messages by PK, while a broad folder (~50%) typically
    benefits from the date-ordered index walk. Neither family is
    "wrong" without a measured regression — so we report the
    distribution and leave the call to the operator.
    """
    if not pairs:
        return "VERDICT (folder-filter): no probes."
    by_family: dict[str, int] = {}
    for _, s in pairs:
        by_family[s.plan_family] = by_family.get(s.plan_family, 0) + 1
    summary = ", ".join(
        f"{count}x {family}"
        for family, count in sorted(by_family.items())
    )
    return (
        f"VERDICT (folder-filter): {len(pairs)} probe(s) — {summary}. "
        f"Option 2 here is selectivity-driven (planner correctly picks "
        f"label-driven access for narrow folders); inspect per-probe "
        f"plan if a specific selectivity surprises you."
    )


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
        "--predicate-form", choices=sorted(_VALID_PREDICATE_FORMS), default="current",
        help=(
            "Mid-keyset cursor predicate to probe. 'current' (default) is "
            "the post-#75 range-seekable form; 'pre75' is the buggy form "
            "with OR COALESCE IS NULL, kept for before/after measurement."
        ),
    )
    parser.add_argument(
        "--folder-filter", action="store_true",
        help=(
            "Seed message_labels rows and add folder-filter probes. "
            "Each account gets a 'selective' mailbox (~5%% of its messages) "
            "and a 'broad' mailbox (~50%%). Four extra probes are appended: "
            "selective+heavy, broad+heavy, broad+heavy mid-keyset, and "
            "broad-across-accounts (#78)."
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
        folders: FolderMailboxes | None = None
        if args.folder_filter:
            folders = _seed_folder_filter_mailboxes(conn, account_ids)
        probes = _build_probes(
            cfg, account_ids, args.page_size, folders=folders,
        )
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
                    "folder_filter": args.folder_filter,
                },
                "probes": [
                    {
                        "name": p.name,
                        "acl_size": len(p.account_ids),
                        "keyset": p.cursor is not None,
                        "folder_filter": p.folder_ids is not None,
                        "folder_count": len(p.folder_ids) if p.folder_ids else 0,
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
                "verdict": _verdict(probes, summaries),
            }, indent=2))
        else:
            print("\n" + _render_table(probes, summaries))
            print("\n" + _verdict(probes, summaries))
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
