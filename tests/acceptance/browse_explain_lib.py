"""Shared library for the browse-explain acceptance harness and pytest tests.

Pure primitives — no CLI, no argparse, no stdout. Two consumers:

1. ``tests/acceptance/run_browse_explain.py`` — operator-facing CLI for
   ad-hoc EXPLAIN runs at production scale (200k+ rows).
2. ``tests/test_browse_at_scale.py`` — CI-gated regression test (#87)
   that asserts the structural plan signature (no ``Unique`` node, no
   full-projection ``Sort``) for the broad-folder probe.

The split exists so that future refactors of ``localmail.api.browse``
land in both consumers automatically: this library composes SQL via the
production primitives (``compose_browse_sql``, ``build_where``,
``BROWSE_ROW_SQL_TEMPLATE``) and never duplicates the SELECT/FROM/WHERE
shape.

Module constants without a leading underscore are public API; the test
and the CLI both read them directly.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from localmail.api.browse import (
    BROWSE_ROW_SQL_TEMPLATE, build_where, compose_browse_sql,
)
from localmail.api.browse_cursor import BrowseCursor


# ---- Seed parameters (public; consumers may override via SeedConfig) ----

EPOCH_ANCHOR = datetime(2024, 1, 1, tzinfo=timezone.utc)
NULL_INTERNAL_DATE_FRAC = 0.10
BOTH_NULL_FRAC = 0.01
DATE_SPAN_DAYS = 365 * 3

DISTRIBUTIONS: dict[str, list[float]] = {
    "balanced": [],
    "skewed":   [0.85],
    "tail":     [0.01, 0.04, 0.05],
}

DEFAULT_PAGE_SIZE = 50

FOLDER_FRACTIONS: dict[str, float] = {
    "selective": 0.05,
    "broad":     0.50,
}

TRUNCATE_SQL = (
    "TRUNCATE accounts, mailboxes, messages, message_labels,"
    " attachment_blobs, failed_messages, message_chunks,"
    " failed_embeddings, embedding_models, failed_chunkings,"
    " attachment_text, attachment_chunks, failed_extractions,"
    " api_users, api_tokens, user_accounts, api_login_attempts"
    " RESTART IDENTITY CASCADE"
)

COPY_BATCH = 5000

VALID_PREDICATE_FORMS = ("current", "pre75")


# ---- Dataclasses --------------------------------------------------------

@dataclass(frozen=True)
class SeedConfig:
    total_rows: int
    num_accounts: int
    distribution: str
    null_internal_frac: float = NULL_INTERNAL_DATE_FRAC
    both_null_frac: float = BOTH_NULL_FRAC
    date_span_days: int = DATE_SPAN_DAYS
    seed: int = 17


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


@dataclass(frozen=True)
class ProbeSpec:
    """One ACL × keyset position combination.

    ``folder_ids`` activates the EXISTS folder-filter predicate inside
    ``build_where`` (#85). ``None`` means the GUI's initial-load path
    (no folder filter).
    """

    name: str
    account_ids: list[int]
    cursor: tuple[datetime, int] | None
    folder_ids: list[int] | None = None


@dataclass(frozen=True)
class FolderMailboxes:
    """Per-account mailbox ids for the two folder-filter probe shapes."""

    selective: list[int]
    broad: list[int]


# ---- Plan classifier ----------------------------------------------------

def classify_plan(explain_text: str) -> PlanSummary:
    """Pick the plan family out of an EXPLAIN ANALYZE textual output.

    Line-oriented and tolerant. Distinction matters: ``Incremental Sort``
    is the cheap DISTINCT tie-breaker on a presorted stream — not a full
    sort over a materialised intermediate. Only a full ``Sort`` node
    (no "Incremental" prefix) signals the planner abandoned the
    date-ordered index. ``Unique`` is the canonical DISTINCT marker;
    a clean EXISTS semi-join never emits one. The two trailing spaces
    in the startswith match Postgres's canonical ``Unique  (cost=...)``
    formatting and avoid spurious matches on any unrelated line whose
    first token happens to begin with "Unique".
    """
    lines = explain_text.splitlines()
    used_recent_idx = any("messages_recent_idx" in ln for ln in lines)
    used_account_idx = any(
        ("messages_acct_date_idx" in ln) or ("messages_acct_msgid_uniq" in ln)
        for ln in lines
    )
    has_incremental_sort = any("Incremental Sort" in ln for ln in lines)
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

    return PlanSummary(
        plan_family=plan_family,
        used_recent_idx=used_recent_idx,
        used_account_idx=used_account_idx,
        has_full_sort=has_full_sort,
        has_incremental_sort=has_incremental_sort,
        has_unique_node=has_unique_node,
        rows_removed_by_filter=_scan_rows_removed_on_messages(lines),
        actual_rows=_scan_actual_rows(lines),
        execution_ms=_scan_timing(lines, "Execution Time:"),
        planning_ms=_scan_timing(lines, "Planning Time:"),
        shared_hit_blocks=_scan_buffers(lines)[0],
        shared_read_blocks=_scan_buffers(lines)[1],
        raw=explain_text,
    )


def _scan_actual_rows(lines: list[str]) -> int:
    """Pull the top-node ``rows=...`` count from an ``EXPLAIN ANALYZE``
    line's ``(actual time=... rows=N loops=M)`` group. Tolerant of
    Postgres >= 18's fractional ``rows=N.NN``."""
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
    """Sum ``Rows Removed by Filter:`` lines under the messages scan."""
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
    """Sum top-level ``Buffers: shared hit=... read=...`` numbers."""
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


# ---- SQL composition (delegates to production primitives) --------------

def initial_page_sql_and_params(
    account_ids: list[int], page_size: int,
    *, folder_ids: list[int] | None = None,
) -> tuple[str, list[Any]]:
    """Compose the initial-page probe SQL + params from production primitives."""
    where, params = build_where(
        account_ids=account_ids, folder_ids=folder_ids, cursor=None,
    )
    return compose_browse_sql(where=where), params + [page_size + 1]


def mid_keyset_sql_and_params(
    account_ids: list[int], ts: datetime, mid: int, page_size: int,
    *, folder_ids: list[int] | None = None,
) -> tuple[str, list[Any]]:
    """Compose the mid-keyset probe SQL + params from production primitives."""
    where, params = build_where(
        account_ids=account_ids, folder_ids=folder_ids,
        cursor=BrowseCursor(ts=ts, id=mid),
    )
    return compose_browse_sql(where=where), params + [page_size + 1]


_PRE75_BUGGY_WHERE = (
    "m.account_id = ANY(%s)"
    " AND (COALESCE(m.internal_date, m.date_sent) < %s"
    "      OR (COALESCE(m.internal_date, m.date_sent) = %s AND m.id < %s)"
    "      OR COALESCE(m.internal_date, m.date_sent) IS NULL)"
)
MID_KEYSET_SQL_PRE75 = BROWSE_ROW_SQL_TEMPLATE.format(where=_PRE75_BUGGY_WHERE)


# ---- Seeding ------------------------------------------------------------

def account_weights(num_accounts: int, distribution: str) -> list[float]:
    """Return per-account row-share weights, sum = 1.0."""
    if distribution not in DISTRIBUTIONS:
        raise ValueError(f"unknown distribution: {distribution}")
    preset = DISTRIBUTIONS[distribution]
    if not preset:
        share = 1.0 / num_accounts
        return [share] * num_accounts
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
        weights = preset[:num_accounts]
        total = sum(weights)
        weights = [w / total for w in weights]
    weights.sort(reverse=True)
    return weights


def seed_accounts(conn: psycopg.Connection, num_accounts: int) -> list[int]:
    """Insert ``num_accounts`` accounts, return their ids."""
    ids: list[int] = []
    with conn.cursor() as cur:
        for i in range(num_accounts):
            cur.execute(
                "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                " VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
                (f"acct{i}", f"acct{i}@example.test"),
            )
            row = cur.fetchone()
            assert row is not None
            ids.append(int(row[0]))
    conn.commit()
    return ids


def seed_messages(
    conn: psycopg.Connection, account_ids: list[int], cfg: SeedConfig,
    *, verbose: bool = False,
) -> None:
    """Bulk-insert messages with account/date shapes matching ``cfg``.

    Uses ``COPY messages FROM STDIN`` for speed — at 100k rows the naive
    ``execute`` round-trip takes minutes. ``verbose`` controls progress
    prints; the CLI sets True, the pytest test False.
    """
    rng = random.Random(cfg.seed)
    weights = account_weights(len(account_ids), cfg.distribution)
    counts = [int(round(w * cfg.total_rows)) for w in weights]
    drift = cfg.total_rows - sum(counts)
    counts[0] += drift

    if verbose:
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
    if verbose:
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
    ts = EPOCH_ANCHOR + timedelta(days=days)
    if coin < cfg.both_null_frac:
        return None, None
    if coin < cfg.both_null_frac + cfg.null_internal_frac:
        return ts, None
    skew = timedelta(hours=rng.uniform(-24, 24))
    return ts + skew, ts


def _synth_sha256(row_idx: int) -> bytes:
    raw = row_idx.to_bytes(8, "big") + b"\x00" * 24
    return raw


def seed_folder_filter_mailboxes(
    conn: psycopg.Connection, account_ids: list[int],
    *, verbose: bool = False,
) -> FolderMailboxes:
    """Create one ``selective`` and one ``broad`` mailbox per account
    and label the requested fraction of each account's messages into
    each (#78). Picks rows in id order so ``broad`` is a strict
    superset of ``selective`` for the same account.
    """
    if verbose:
        print(f"  seeding folder-filter mailboxes + labels for "
              f"{len(account_ids)} account(s)…", flush=True)
    selective: list[int] = []
    broad: list[int] = []
    t0 = time.monotonic()
    with conn.cursor() as cur:
        for aid in account_ids:
            for name, fraction in FOLDER_FRACTIONS.items():
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
    if verbose:
        print(f"  seeded folder-filter rows in {time.monotonic() - t0:.1f}s",
              flush=True)
    return FolderMailboxes(selective=selective, broad=broad)


# ---- Probes -------------------------------------------------------------

def mid_cursor_from_seed(cfg: SeedConfig) -> tuple[datetime, int]:
    """Derive a mid-keyset cursor directly from the seed config (#79)."""
    mid_ts = EPOCH_ANCHOR + timedelta(days=cfg.date_span_days / 2)
    mid_id = cfg.total_rows // 2
    return (mid_ts, mid_id)


def build_probes(
    cfg: SeedConfig, account_ids: list[int], page_size: int,
    *, folders: FolderMailboxes | None = None,
) -> list[ProbeSpec]:
    """Build the operator-facing probe matrix.

    Four ACL widths × two keyset positions, optionally crossed with
    four folder-filter probes when ``folders`` is supplied.
    """
    if not account_ids:
        return []
    heavy = [account_ids[0]]
    light = [account_ids[-1]]
    half = account_ids[: max(1, len(account_ids) // 2)]
    everything = list(account_ids)
    cursor_pos = mid_cursor_from_seed(cfg)
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
        out.extend(build_folder_filter_probes(account_ids, cursor_pos, folders))
    return out


def build_folder_filter_probes(
    account_ids: list[int],
    cursor_pos: tuple[datetime, int],
    folders: FolderMailboxes,
) -> list[ProbeSpec]:
    """Folder-filter probes (#78). Four shapes."""
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


def run_explain(
    conn: psycopg.Connection, probe: ProbeSpec, page_size: int,
    *, predicate_form: str = "current",
) -> PlanSummary:
    """Run EXPLAIN (ANALYZE, BUFFERS, VERBOSE) and classify the plan.

    ``predicate_form`` selects the mid-keyset SQL: ``"current"`` (default,
    post-#75) or ``"pre75"`` (buggy form for operator before/after).
    """
    if predicate_form not in VALID_PREDICATE_FORMS:
        raise ValueError(
            f"unknown predicate_form: {predicate_form!r}; "
            f"choose from {VALID_PREDICATE_FORMS}"
        )
    if probe.cursor is None:
        sql, params = initial_page_sql_and_params(
            probe.account_ids, page_size, folder_ids=probe.folder_ids,
        )
    else:
        ts, mid = probe.cursor
        if predicate_form == "current":
            sql, params = mid_keyset_sql_and_params(
                probe.account_ids, ts, mid, page_size,
                folder_ids=probe.folder_ids,
            )
        else:
            if probe.folder_ids:
                raise ValueError(
                    "predicate_form='pre75' is not combined with "
                    "folder filtering — the pre-#75 bug is orthogonal"
                )
            sql = MID_KEYSET_SQL_PRE75
            params = [probe.account_ids, ts, ts, mid, page_size + 1]
    explain_sql = "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT TEXT) " + sql
    with conn.cursor() as cur:
        cur.execute(explain_sql, params)
        rows = cur.fetchall()
    explain_text = "\n".join(r[0] for r in rows)
    return classify_plan(explain_text)
