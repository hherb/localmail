# Browse at-scale folder-filter regression coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CI-gated pytest test that catches the `Unique`-node DISTINCT regression in `localmail.api.browse`'s folder-filter path at scale, plus the supporting Python CI workflow this repo needs to gate it.

**Architecture:** Refactor the existing operator-run acceptance harness `tests/acceptance/run_browse_explain.py` to extract its pure primitives (seed config, seeding functions, probe builders, `classify_plan`) into a shared library at `tests/acceptance/browse_explain_lib.py`. Add a new pytest test `tests/test_browse_at_scale.py` that consumes the library, seeds a calibrated archive, and asserts the structural plan signature (no `Unique` node, no full-projection Sort) on the broad-folder probe. Add a new `.github/workflows/python-ci.yml` with a `pgvector/pgvector:pg18` service container running the full pytest suite.

**Tech Stack:** Python 3.12, pytest, psycopg v3, PostgreSQL 18 + pgvector, GitHub Actions, `uv` for dep management.

**Spec:** [docs/superpowers/specs/2026-05-26-browse-at-scale-regression-design.md](../specs/2026-05-26-browse-at-scale-regression-design.md)

**Branch:** `issue-87-at-scale-folder-filter-regression-coverage` (already created off `main`; spec commit `29a470f`).

---

## File map (post-implementation)

```
docs/superpowers/
  specs/2026-05-26-browse-at-scale-regression-design.md   # already committed
  plans/2026-05-26-browse-at-scale-regression.md          # this file

.github/workflows/
  python-ci.yml                                           # NEW (Task 5)

tests/
  test_browse_at_scale.py                                 # NEW (Task 4)
  test_browse_explain_classifier.py                       # NEW (Task 1)
  acceptance/
    browse_explain_lib.py                                 # NEW (Task 2/3)
    run_browse_explain.py                                 # REFACTORED (Task 3)
```

No other files touched. No migrations, no `src/localmail/` changes.

---

## Task 0: Pre-flight smoke test

Sanity-check that the existing harness still runs on `main` so we have a baseline for the refactor.

**Files:**
- Read: `tests/acceptance/run_browse_explain.py`

- [ ] **Step 1: Confirm Postgres is up**

```bash
psql "postgresql://localmail:local%40%40mail@localhost:5532/localmail_test" -c "SELECT 1"
```

Expected: `1` returned. If this fails, start the local test DB before continuing.

- [ ] **Step 2: Baseline-run the existing operator harness at a small scale**

```bash
cd /Users/hherb/src/localmail
unset VIRTUAL_ENV
LOCALMAIL_TEST_DSN="postgresql://localmail:local%40%40mail@localhost:5532/localmail_test" \
  PYTHONPATH=src:. \
  uv run python tests/acceptance/run_browse_explain.py \
  --total-rows 5000 --accounts 3 --folder-filter --json
```

Expected: JSON output ending with a `verdict` line. Save this to `/tmp/baseline_before_refactor.json`:

```bash
LOCALMAIL_TEST_DSN="postgresql://localmail:local%40%40mail@localhost:5532/localmail_test" \
  PYTHONPATH=src:. \
  uv run python tests/acceptance/run_browse_explain.py \
  --total-rows 5000 --accounts 3 --folder-filter --json \
  > /tmp/baseline_before_refactor.json
```

We compare against this in Task 3 step 7.

- [ ] **Step 3: Run the existing pytest suite**

```bash
unset VIRTUAL_ENV && uv run pytest -q
```

Expected: existing tests pass. Note the pass count — we want it unchanged after the refactor (modulo the new tests we add).

No commit at this task — it's purely diagnostic.

---

## Task 1: Extend `PlanSummary` with `has_unique_node` and unit-test the classifier

The new pytest test needs a direct boolean for "is there a `Unique` node?". Today's `classify_plan` doesn't track this. We add the field and TDD it with a unit test on synthetic EXPLAIN strings — no DB needed.

This task lands *before* the refactor in Task 2/3 because the new field belongs in the library we're about to extract, and TDD says the test comes first.

**Files:**
- Modify: `tests/acceptance/run_browse_explain.py` (extend `PlanSummary` and `classify_plan`)
- Create: `tests/test_browse_explain_classifier.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_browse_explain_classifier.py`:

```python
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
```

- [ ] **Step 2: Run the test and verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_browse_explain_classifier.py -v
```

Expected: FAIL with `AttributeError: 'PlanSummary' object has no attribute 'has_unique_node'` (or a `TypeError` if classify_plan's constructor call breaks first).

- [ ] **Step 3: Add the field to `PlanSummary`**

Edit `tests/acceptance/run_browse_explain.py`. Find the `PlanSummary` dataclass (around line 233-248) and add `has_unique_node: bool` between `has_incremental_sort` and `rows_removed_by_filter`:

```python
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
```

- [ ] **Step 4: Populate `has_unique_node` in `classify_plan`**

Edit `tests/acceptance/run_browse_explain.py`. In `classify_plan` (around line 266-311), after the line that sets `has_incremental_sort`, add:

```python
    has_unique_node = any(
        ln.strip().startswith("Unique") or ln.strip().startswith("->  Unique")
        for ln in lines
    )
```

Then update the `PlanSummary(...)` constructor call at the bottom of the function to include the new field:

```python
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
```

- [ ] **Step 5: Run the unit tests to verify they pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_browse_explain_classifier.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 6: Run the full pytest suite — nothing else should break**

```bash
unset VIRTUAL_ENV && uv run pytest -q
```

Expected: all previously-passing tests still pass, plus the 3 new ones.

- [ ] **Step 7: Commit**

```bash
git add tests/acceptance/run_browse_explain.py tests/test_browse_explain_classifier.py
git commit -m "$(cat <<'EOF'
test(acceptance): track has_unique_node in PlanSummary (#87 prep)

The at-scale folder-filter regression test in the next commit needs a
direct boolean for "is there a Unique node?" so it can assert that the
DISTINCT-regression class can't silently come back through a refactor
of build_where. The Unique-detection heuristic is line-based, matching
the existing has_full_sort heuristic.

Unit tests in test_browse_explain_classifier.py verify both shapes
Postgres emits (top-level ``Unique`` and indented ``->  Unique``).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Extract the shared library `browse_explain_lib.py`

Move the pure primitives out of `run_browse_explain.py` and into a new library module. The CLI becomes a thin wrapper in Task 3. This task only *creates* the library — the CLI keeps its own copy until Task 3 deletes them and switches to importing from the library.

(We do the move in two commits — extract + switch — because the first commit must keep the CLI working without behavioural change so we can sanity-check via the baseline JSON.)

**Files:**
- Create: `tests/acceptance/browse_explain_lib.py`

- [ ] **Step 1: Create the library file**

Create `tests/acceptance/browse_explain_lib.py`:

```python
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
    a clean EXISTS semi-join never emits one.
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
        ln.strip().startswith("Unique") or ln.strip().startswith("->  Unique")
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
    Postgres ≥18's fractional ``rows=N.NN``."""
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


# Pre-#75 buggy form kept for ad-hoc operator before/after measurement.
# Not exercised by the regression test; the bug it reproduces is
# orthogonal to the #87 regression class.
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
            row = cur.fetchone(); assert row is not None
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
```

- [ ] **Step 2: Verify the library imports cleanly**

```bash
unset VIRTUAL_ENV && uv run python -c "from tests.acceptance.browse_explain_lib import classify_plan, seed_accounts, run_explain; print('ok')"
```

Expected: `ok` printed. If `ImportError`, fix it before committing.

- [ ] **Step 3: Run the existing classifier unit tests**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_browse_explain_classifier.py -v
```

Expected: 3 tests PASS — still using `run_browse_explain.classify_plan`. The library exists in parallel; we haven't switched the test or the CLI yet.

- [ ] **Step 4: Commit the library-only addition**

```bash
git add tests/acceptance/browse_explain_lib.py
git commit -m "$(cat <<'EOF'
test(acceptance): extract pure primitives into browse_explain_lib (#87)

New library module with SeedConfig/PlanSummary/ProbeSpec/FolderMailboxes
dataclasses, classify_plan, seed_*, and SQL composition helpers. Pure;
no CLI, no argparse, no stdout. The CLI in run_browse_explain.py still
carries its own copies — the switchover is in the next commit so the
operator harness stays bit-identical for one commit while the library
gets reviewed in isolation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Switch `run_browse_explain.py` to import from the library

Now make the CLI a thin consumer. The behavioural contract: identical JSON output for the same args.

**Files:**
- Modify: `tests/acceptance/run_browse_explain.py` (gut and replace with thin CLI)
- Modify: `tests/test_browse_explain_classifier.py` (switch import to the library)

- [ ] **Step 1: Switch the classifier unit test to import from the library**

Edit `tests/test_browse_explain_classifier.py`. Change all three `from run_browse_explain import classify_plan` lines to `from browse_explain_lib import classify_plan`. The `sys.path` shim at the top stays the same.

- [ ] **Step 2: Run the classifier unit tests against the library**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_browse_explain_classifier.py -v
```

Expected: 3 tests PASS. Verifies the library's classifier is correct *before* we remove the duplicate from the CLI.

- [ ] **Step 3: Rewrite `run_browse_explain.py` as a thin CLI**

Replace the entire contents of `tests/acceptance/run_browse_explain.py` with:

```python
"""Measure the planner choice for ``localmail.api.browse.list_messages``.

This is the operator-facing CLI; the pure primitives live in
``browse_explain_lib.py``. See that module's docstring for the full
explanation of the probe matrix and the plan classifier.

Usage::

    LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test \\
      PYTHONPATH=src:. uv run python tests/acceptance/run_browse_explain.py \\
      --total-rows 100000 --accounts 5

Optional flags:

* ``--distribution {balanced,skewed,tail}`` (default ``skewed``)
* ``--page-size N`` (default 50)
* ``--predicate-form {current,pre75}`` — choose the mid-keyset cursor
  predicate. ``current`` is the post-#75 range-seekable form; ``pre75``
  is the buggy OR-form kept for ad-hoc before/after measurement.
* ``--folder-filter`` — add folder-filter probes (#78)
* ``--keep-data`` — leave the seeded rows in place
* ``--json`` — machine-readable summary

Prerequisites: a reachable Postgres at ``LOCALMAIL_TEST_DSN``, schema at
migration 0019 or later. The script TRUNCATEs the listed tables before
seeding — never run against a live archive.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# tests/acceptance is not a package; add its directory to sys.path so
# the library module can be imported when this script is run directly.
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import psycopg

from browse_explain_lib import (
    DEFAULT_PAGE_SIZE,
    DISTRIBUTIONS,
    FolderMailboxes,
    PlanSummary,
    ProbeSpec,
    SeedConfig,
    TRUNCATE_SQL,
    VALID_PREDICATE_FORMS,
    build_probes,
    run_explain,
    seed_accounts,
    seed_folder_filter_mailboxes,
    seed_messages,
)

from localmail.db import apply_migrations


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
    """Multi-line verdict split by folder-filter dimension."""
    folderless = [
        (p, s) for p, s in zip(probes, summaries) if p.folder_ids is None
    ]
    folder_filter = [
        (p, s) for p, s in zip(probes, summaries) if p.folder_ids is not None
    ]
    lines: list[str] = [_verdict_for_folderless(folderless)]
    if folder_filter:
        lines.append(_verdict_for_folder_filter(folder_filter))
    return "\n".join(lines)


def _verdict_for_folderless(
    pairs: list[tuple[ProbeSpec, PlanSummary]],
) -> str:
    """Folderless verdict — covering-index recommendation (#72)."""
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
    """Folder-filter verdict — informational only (#78)."""
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


def main() -> int:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--total-rows", type=int, default=100_000)
    parser.add_argument("--accounts", type=int, default=5)
    parser.add_argument(
        "--distribution", choices=sorted(DISTRIBUTIONS), default="skewed",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument(
        "--predicate-form", choices=sorted(VALID_PREDICATE_FORMS),
        default="current",
        help=(
            "Mid-keyset cursor predicate to probe. 'current' (default) is "
            "the post-#75 range-seekable form; 'pre75' is the buggy form "
            "with OR COALESCE IS NULL, kept for before/after measurement."
        ),
    )
    parser.add_argument(
        "--folder-filter", action="store_true",
        help="Seed message_labels and add folder-filter probes (#78).",
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
            cur.execute(TRUNCATE_SQL)
        conn.commit()

        cfg = SeedConfig(
            total_rows=args.total_rows,
            num_accounts=args.accounts,
            distribution=args.distribution,
        )
        account_ids = seed_accounts(conn, args.accounts)
        seed_messages(conn, account_ids, cfg, verbose=True)
        folders: FolderMailboxes | None = None
        if args.folder_filter:
            folders = seed_folder_filter_mailboxes(
                conn, account_ids, verbose=True,
            )
        probes = build_probes(
            cfg, account_ids, args.page_size, folders=folders,
        )
        if not probes:
            print("no probes generated (no accounts?)", file=sys.stderr)
            return 1

        summaries: list[PlanSummary] = []
        for probe in probes:
            summ = run_explain(
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
                cur.execute(TRUNCATE_SQL)
            conn.commit()

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Verify the CLI still runs and produces a verdict**

```bash
cd /Users/hherb/src/localmail
unset VIRTUAL_ENV
LOCALMAIL_TEST_DSN="postgresql://localmail:local%40%40mail@localhost:5532/localmail_test" \
  PYTHONPATH=src:. \
  uv run python tests/acceptance/run_browse_explain.py \
  --total-rows 5000 --accounts 3 --folder-filter --json \
  > /tmp/baseline_after_refactor_raw.txt
```

Expected: file written. No errors.

Then extract just the JSON object (the harness intermixes progress
`print()` lines with the JSON payload):

```bash
python3 -c "
import json
with open('/tmp/baseline_after_refactor_raw.txt') as f:
    text = f.read()
start = text.find('{\n')
depth = 0
end = -1
for i in range(start, len(text)):
    if text[i] == '{':
        depth += 1
    elif text[i] == '}':
        depth -= 1
        if depth == 0:
            end = i + 1
            break
data = json.loads(text[start:end])
with open('/tmp/baseline_after_refactor.json', 'w') as f:
    json.dump(data, f, indent=2)
print('saved', len(data['probes']), 'probes')
"
```

The pre-refactor baseline at `/tmp/baseline_before_refactor.json` was
already extracted via the same script during Task 0 — both files now
contain pure JSON.

- [ ] **Step 5: Diff against the pre-refactor baseline**

```bash
diff <(jq 'del(.probes[].execution_ms, .probes[].planning_ms, .probes[].buf_hit, .probes[].buf_read, .probes[].actual_rows, .probes[].rows_removed_by_filter)' /tmp/baseline_before_refactor.json) \
     <(jq 'del(.probes[].execution_ms, .probes[].planning_ms, .probes[].buf_hit, .probes[].buf_read, .probes[].actual_rows, .probes[].rows_removed_by_filter)' /tmp/baseline_after_refactor.json)
```

Expected: no diff. We strip the perf-counter fields (which vary run-to-run on the same data) and the actual_rows/rows_removed (planner-estimate jitter); the plan-family verdict and probe shape must be identical.

If there *is* a diff, investigate before continuing — the refactor changed behaviour.

- [ ] **Step 6: Run the full pytest suite**

```bash
unset VIRTUAL_ENV && uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add tests/acceptance/run_browse_explain.py tests/test_browse_explain_classifier.py
git commit -m "$(cat <<'EOF'
test(acceptance): switch run_browse_explain.py to import from lib (#87)

The CLI is now ~300 lines of argparse + reporting + verdict; all
primitives import from browse_explain_lib. JSON output (modulo
perf-counter jitter) is bit-identical to pre-refactor, verified
against /tmp/baseline_before_refactor.json.

The classifier unit test also switches to the library import.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Add `tests/test_browse_at_scale.py` with a deliberately-large scale

Write the regression test against the calibrated archive. Initial `DEFAULT_REGRESSION_ROWS = 50_000` — deliberately over-calibrated. We narrow it down in Task 5.

**Files:**
- Create: `tests/test_browse_at_scale.py`

- [ ] **Step 1: Write the test file**

Create `tests/test_browse_at_scale.py`:

```python
"""At-scale regression coverage for the broad-folder browse plan family (#87).

Pins that the DISTINCT-regression signature (`Unique` node plus
full-projection Sort on top of a Nested Loop) cannot silently come back
through a refactor of ``localmail.api.browse.build_where``.

Sits between two existing layers:
* ``tests/test_api_browse_plan.py`` — unit-scale eligibility tests
  (fixture scale; deliberately permit a Sort because the planner
  inverts the semi-join at fixture scale).
* ``tests/acceptance/run_browse_explain.py`` — operator-run harness at
  200k+ rows (catches this class but is not CI-gated).

The test seeds a calibrated archive (3 accounts × N rows, broad folder
labelling 50% of each account) and asserts that EXPLAIN on the
broad-folder probe shows:

1. ``Index Scan using messages_recent_idx on messages``.
2. No ``Unique`` node — the canonical DISTINCT marker. A clean EXISTS
   semi-join never emits one.
3. No full-projection ``Sort`` on top of a Nested Loop — the legitimate
   inverted-semi-join Sort sits at sub-calibration scale, ruled out by
   the calibration gate.

The calibration gate runs first: if the planner picks a non-date-ordered
walk, the regression class can't surface at that scale and the test
fails fast with a hint to bump ``LOCALMAIL_REGRESSION_ROWS``.

Scale tunable via env var:
* ``LOCALMAIL_REGRESSION_ROWS`` — override the default row count.

Auto-skips when no DB is reachable (via the standard ``db_conn`` fixture).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import psycopg
import pytest

# tests/acceptance is not a package; add it to sys.path so we can
# import the shared library.
_ACCEPTANCE_DIR = Path(__file__).parent / "acceptance"
if str(_ACCEPTANCE_DIR) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE_DIR))

from browse_explain_lib import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    ProbeSpec,
    SeedConfig,
    run_explain,
    seed_accounts,
    seed_folder_filter_mailboxes,
    seed_messages,
)


# Calibrated scale at which the planner reliably picks the date-ordered
# walk for the broad-folder probe (50% labelled, 3 accounts, balanced
# distribution). Below this scale the planner inverts the semi-join —
# legitimate, but the #87 regression class can't surface, so the
# calibration gate fails. Operators with a slow CI runner can lower
# this via LOCALMAIL_REGRESSION_ROWS at the cost of the calibration
# gate possibly failing on PG planner cost-model drift. Calibrated
# against PG 18.1 on 2026-05-26; see Task 5 of the implementation plan.
DEFAULT_REGRESSION_ROWS = 50_000

_NUM_ACCOUNTS = 3


def _resolved_row_count() -> int:
    """Read ``LOCALMAIL_REGRESSION_ROWS`` or fall back to the default."""
    raw = os.environ.get("LOCALMAIL_REGRESSION_ROWS")
    if raw is None:
        return DEFAULT_REGRESSION_ROWS
    try:
        n = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"LOCALMAIL_REGRESSION_ROWS must be an integer, got {raw!r}"
        ) from exc
    if n < 1000:
        raise ValueError(
            f"LOCALMAIL_REGRESSION_ROWS must be at least 1000 to be "
            f"meaningful, got {n}"
        )
    return n


def test_broad_folder_filter_does_not_regress_to_distinct_plan_family(
    db_conn: psycopg.Connection, caplog: pytest.LogCaptureFixture,
) -> None:
    """The broad-folder probe must not produce a ``Unique`` node or a
    full-projection ``Sort`` over the messages projection at scale.

    Calibration gate runs first: the planner must pick the date-ordered
    walk (plan family ``"index-walk (option 1)"``). If it picks the
    inverted semi-join, the scale is below the regression-detection
    threshold and the test fails with a hint to bump
    LOCALMAIL_REGRESSION_ROWS.

    Once calibrated, the signature assertion catches the #87 regression
    class (DISTINCT re-introduced; EXISTS swapped for IN (SELECT ...);
    any change that forces the planner to dedup on the messages side).
    """
    caplog.set_level(logging.INFO, logger=__name__)
    log = logging.getLogger(__name__)

    n_rows = _resolved_row_count()
    cfg = SeedConfig(
        total_rows=n_rows,
        num_accounts=_NUM_ACCOUNTS,
        distribution="balanced",
    )

    account_ids = seed_accounts(db_conn, _NUM_ACCOUNTS)
    seed_messages(db_conn, account_ids, cfg, verbose=False)
    folders = seed_folder_filter_mailboxes(db_conn, account_ids, verbose=False)
    first_account_id = account_ids[0]
    broad_mailbox_id = folders.broad[0]

    probe = ProbeSpec(
        name="broad folder initial page",
        account_ids=[first_account_id],
        cursor=None,
        folder_ids=[broad_mailbox_id],
    )
    summary = run_explain(db_conn, probe, page_size=DEFAULT_PAGE_SIZE)

    log.info(
        "at-scale broad-folder probe: rows=%d, plan_family=%r, "
        "exec_ms=%.2f, buf_hit=%d, buf_read=%d",
        n_rows, summary.plan_family,
        summary.execution_ms,
        summary.shared_hit_blocks, summary.shared_read_blocks,
    )

    # ---- Calibration gate ------------------------------------------------
    assert summary.plan_family == "index-walk (option 1)", (
        f"calibration gate failed: planner picked plan family "
        f"{summary.plan_family!r} at {n_rows} rows. The #87 regression "
        f"class (Unique + full Sort) can only surface when the planner "
        f"prefers the date-ordered walk. Bump LOCALMAIL_REGRESSION_ROWS "
        f"or investigate a PG planner cost-model change.\n\n"
        f"Raw EXPLAIN:\n{summary.raw}"
    )

    # ---- Regression signature assertions --------------------------------
    assert "Index Scan using messages_recent_idx" in summary.raw, (
        f"messages_recent_idx no longer used for the broad-folder probe.\n\n"
        f"Raw EXPLAIN:\n{summary.raw}"
    )
    assert not summary.has_unique_node, (
        f"Unique node detected — DISTINCT semantics have come back through "
        f"a refactor of build_where (#87 regression class).\n\n"
        f"Raw EXPLAIN:\n{summary.raw}"
    )
    assert not summary.has_full_sort, (
        f"Full Sort node detected at calibrated scale — the planner has "
        f"abandoned the date-ordered walk despite the calibration gate "
        f"passing. Likely a new plan family; inspect raw EXPLAIN.\n\n"
        f"Raw EXPLAIN:\n{summary.raw}"
    )
```

- [ ] **Step 2: Run the new test against the live local DB**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_browse_at_scale.py -v
```

Expected: PASS within ~10–30 seconds. The seed COPY + ANALYZE + EXPLAIN ANALYZE round-trip on 50k rows should complete well under a minute.

If the calibration gate fails with `plan_family='other'` or `plan_family='index + full sort (degraded option 1)'`, **stop** — the 50k starting value isn't enough. Bump `LOCALMAIL_REGRESSION_ROWS=75000` and retry. Update `DEFAULT_REGRESSION_ROWS` in the file accordingly before proceeding.

If the calibration gate passes but a signature assertion fails, **stop** — that's a real regression in the current code, not a calibration issue. Investigate before continuing.

- [ ] **Step 3: Run the full pytest suite**

```bash
unset VIRTUAL_ENV && uv run pytest -q
```

Expected: all tests pass, including the new one.

- [ ] **Step 4: Commit**

```bash
git add tests/test_browse_at_scale.py
git commit -m "$(cat <<'EOF'
test: at-scale folder-filter regression coverage (#87)

Seeds a calibrated archive (3 accounts × N rows × broad mailbox at 50%)
and asserts the structural plan signature for the broad-folder probe:
Index Scan using messages_recent_idx, no Unique node, no full-projection
Sort. The calibration gate runs first — if the planner inverts the
semi-join (legitimate at sub-calibration scale), the test fails with
a hint to bump LOCALMAIL_REGRESSION_ROWS rather than emitting a
vacuously-green signature assertion.

DEFAULT_REGRESSION_ROWS = 50_000 is the initial calibration; the next
commit narrows this to the smallest stable value.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Calibrate `DEFAULT_REGRESSION_ROWS` downward

Find the smallest `N` that reliably picks the date-ordered walk across 5 consecutive runs. The smaller, the faster CI. We accept some headroom (1.5×) to absorb PG planner cost-model jitter across runs.

**Files:**
- Modify: `tests/test_browse_at_scale.py:DEFAULT_REGRESSION_ROWS`

- [ ] **Step 1: Run the test at progressively smaller `N` values**

For each candidate in `40000, 30000, 20000, 15000, 12000, 10000`:

```bash
unset VIRTUAL_ENV
for n in 40000 30000 20000 15000 12000 10000; do
  echo "=== N=$n ==="
  for run in 1 2 3 4 5; do
    LOCALMAIL_REGRESSION_ROWS=$n \
      uv run pytest tests/test_browse_at_scale.py -q 2>&1 | tail -3
  done
done
```

Expected: each candidate produces 5 PASS or 5 FAIL outputs (rare partial-flip is possible at the boundary).

- [ ] **Step 2: Pick the smallest stable `N` and apply 1.5× headroom**

The smallest `N` where all 5 runs PASSed is `N_stable`. Set:

```
DEFAULT_REGRESSION_ROWS = ceil(N_stable * 1.5)
```

(Round up to the nearest 1000 for readability — e.g., if `N_stable = 12_000`, set `DEFAULT_REGRESSION_ROWS = 18_000`.)

- [ ] **Step 3: Update `DEFAULT_REGRESSION_ROWS` in the file**

Edit `tests/test_browse_at_scale.py`. Change the module constant:

```python
DEFAULT_REGRESSION_ROWS = <chosen value>  # calibrated 2026-05-26 against PG 18.1
```

Update the docstring above the constant to note the calibrated value, the date, and the PG version.

- [ ] **Step 4: Verify the test passes at the new default**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_browse_at_scale.py -v
```

Expected: PASS in the smaller runtime budget.

- [ ] **Step 5: Run the full pytest suite to confirm nothing else broke**

```bash
unset VIRTUAL_ENV && uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_browse_at_scale.py
git commit -m "$(cat <<'EOF'
test: calibrate DEFAULT_REGRESSION_ROWS for the broad-folder probe (#87)

Empirically determined smallest stable N (5 consecutive PASSes on the
calibration gate) × 1.5 headroom multiplier against PG 18.1 on
2026-05-26. Smaller N keeps CI fast; the headroom absorbs PG planner
cost-model jitter run-to-run.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Add `.github/workflows/python-ci.yml`

Stand up the Python CI workflow so the new regression test (and the rest of the pytest suite) actually gates PRs.

**Files:**
- Create: `.github/workflows/python-ci.yml`

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/python-ci.yml`:

```yaml
name: python-ci

on:
  push:
    branches: [main]
    paths:
      - 'src/**'
      - 'tests/**'
      - 'migrations/**'
      - 'pyproject.toml'
      - 'uv.lock'
      - '.github/workflows/python-ci.yml'
  pull_request:
    paths:
      - 'src/**'
      - 'tests/**'
      - 'migrations/**'
      - 'pyproject.toml'
      - 'uv.lock'
      - '.github/workflows/python-ci.yml'

jobs:
  pytest:
    name: pytest (PG ${{ matrix.postgres }}, Python ${{ matrix.python }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        postgres: ["pg18"]
        python: ["3.12"]
    services:
      postgres:
        image: pgvector/pgvector:${{ matrix.postgres }}
        env:
          POSTGRES_USER: localmail
          POSTGRES_PASSWORD: 'local@@mail'
          POSTGRES_DB: localmail_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 10
    env:
      LOCALMAIL_TEST_DSN: postgresql://localmail:local%40%40mail@localhost:5432/localmail_test
    steps:
      - uses: actions/checkout@v6
      - name: Install uv
        uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
      - name: Pin Python via uv
        run: uv python install ${{ matrix.python }}
      - name: Sync dependencies
        run: uv sync --frozen
      - name: Run pytest
        run: uv run pytest -q
```

Key choices:
- `pgvector/pgvector:pg18` covers the `CREATE EXTENSION IF NOT EXISTS vector` requirement in migration 0004.
- `astral-sh/setup-uv@v6` matches the action major version we just bumped to in #97/PR #98 — same Node 24 runtime, same caching primitives.
- Postgres on port 5432 inside the container; the host-side DSN reaches localhost:5432 because GH Actions services expose the port directly.

- [ ] **Step 2: Verify yaml syntax locally**

```bash
unset VIRTUAL_ENV && uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/python-ci.yml'))" && echo ok
```

Expected: `ok` printed. (`yaml` is a transitive dep of e.g. `docling`/`pikepdf`; if not present, install it via `uv run --with pyyaml python ...`.)

- [ ] **Step 3: Verify the workflow file is recognised by `gh`**

```bash
gh workflow list 2>&1 | head
```

Expected: list of workflows (or "no workflows found" if `gh` filters by registered runs — not an error).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/python-ci.yml
git commit -m "$(cat <<'EOF'
ci(python): add python-ci.yml with pgvector pg18 service (#87)

Runs the full pytest suite on every push to main and every PR touching
src/, tests/, migrations/, pyproject.toml, uv.lock, or the workflow
itself. Postgres provided by pgvector/pgvector:pg18 so migration 0004's
CREATE EXTENSION vector clause works without an extra setup step.

Pinned to one PG version (pg18, matching dev env) and one Python
version (3.12, matching pyproject.toml's requires-python). Matrix
syntax in place so a future contributor can broaden either dimension
in one line.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Push, watch CI, fix issues

The workflow's first real test is its own run. Likely issues: pgvector image quirks, port mismatches, dep-install hiccups.

**Files:** none (CI iteration only).

- [ ] **Step 1: Push the branch to origin**

```bash
git push -u origin issue-87-at-scale-folder-filter-regression-coverage
```

Expected: branch pushed; PR creation URL printed.

- [ ] **Step 2: Open a draft PR linked to #87**

```bash
gh pr create --draft --title "test: at-scale folder-filter regression coverage + python-ci (#87)" --body "$(cat <<'EOF'
## Summary

- Adds `tests/test_browse_at_scale.py` — CI-gated pytest test that catches the DISTINCT-regression class (Unique node + full Sort on the messages projection) introduced by a future refactor of `localmail.api.browse.build_where`. Closes #87.
- Extracts the operator harness's pure primitives into a shared library at `tests/acceptance/browse_explain_lib.py` so production-SQL drift can't bypass the test (#77/#85 invariant).
- Adds `.github/workflows/python-ci.yml` — first Python CI workflow in this repo. Runs the full pytest suite against a `pgvector/pgvector:pg18` service container on every push to main and every PR touching Python code.

Design: [docs/superpowers/specs/2026-05-26-browse-at-scale-regression-design.md](docs/superpowers/specs/2026-05-26-browse-at-scale-regression-design.md)
Plan:   [docs/superpowers/plans/2026-05-26-browse-at-scale-regression.md](docs/superpowers/plans/2026-05-26-browse-at-scale-regression.md)

## Test plan

- [ ] `uv run pytest` green locally
- [ ] `python-ci` workflow green on this PR
- [ ] Operator harness JSON unchanged vs pre-refactor (`tests/acceptance/run_browse_explain.py --total-rows 5000 --accounts 3 --folder-filter --json`)
- [ ] At-scale calibration: plan family is `index-walk (option 1)` for the broad-folder probe at `DEFAULT_REGRESSION_ROWS`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

- [ ] **Step 3: Watch the CI run**

```bash
gh pr checks --watch
```

Expected: all checks (gui-ci + new python-ci) green within 5-10 minutes.

- [ ] **Step 4: If `python-ci` fails, diagnose**

```bash
gh run view --log-failed | tail -200
```

Common failure modes and fixes (do not change pre-emptively):

| Failure | Fix |
|---|---|
| `uv sync --frozen` fails: lockfile out of date | Remove `--frozen` locally, run `uv lock`, commit lockfile, re-push |
| `pg_isready` healthcheck times out | Increase `--health-retries` to 20 |
| Migration 0004 `CREATE EXTENSION vector` fails | Confirm the image tag is `pgvector/pgvector:pg18`, not `postgres:18` |
| Calibration gate fails on the GH runner (different cost model) | Bump `DEFAULT_REGRESSION_ROWS` to 1.5×-2× the value calibrated locally; commit + push |
| Some other test fails | Fix it; this is the first run of any of these tests under CI |

- [ ] **Step 5: Once CI is green, flip the PR to ready-for-review**

```bash
gh pr ready
```

Expected: PR marked as ready-for-review. Then it's on the maintainer to merge.

No commit at this task end — push is the artefact.

---

## Self-review

Spec coverage check:

- ✅ Spec §"Scope" item 1 (Python CI workflow) → Task 6
- ✅ Spec §"Scope" item 2 (refactor `run_browse_explain.py` into a library) → Tasks 2 + 3
- ✅ Spec §"Scope" item 3 (new pytest test, `LOCALMAIL_REGRESSION_ROWS` env var) → Tasks 4 + 5
- ✅ Spec §"Component 1" — library has every named export → Task 2 listing matches
- ✅ Spec §"Component 2" — thin CLI, every flag preserved → Task 3 Step 3 lists each flag
- ✅ Spec §"Component 3" — test does 8 things → Task 4 Step 1 covers calibration gate, plan-family + Unique + Sort assertions, observability
- ✅ Spec §"Component 4" — workflow choices (pgvector pg18, frozen uv, 3.12 Python) → Task 6
- ✅ Spec §"Calibration step" → Task 5
- ✅ Spec §"Error handling" rows: no DB skip (existing fixture), service container fail-loud (workflow), new plan family ("other" classification) → Task 4 calibration gate text
- ✅ Spec §"has_unique_node" addition to PlanSummary → Task 1 (TDD'd with unit test before the library refactor)

Placeholder scan: no TBD / TODO / "fill in later" found.

Type consistency: `PlanSummary.has_unique_node` defined in Task 1, consumed in Task 4 — fields and types match. `ProbeSpec(name=..., account_ids=..., cursor=..., folder_ids=...)` — same keyword args used in Task 4 Step 1 (test) as in Task 2 Step 1 (library dataclass definition). `seed_accounts(conn, n) -> list[int]`, `seed_messages(conn, ids, cfg, *, verbose=...)` — signatures match between Task 2 (definition) and Task 4 (call site).

Frequent commits: 6 commit points across 7 tasks (Task 0 + Task 7 don't commit). All commits leave the tree green.

DRY: the library has one definition of `classify_plan`, `SeedConfig`, `PlanSummary`, etc. The CLI and the test both import. The pre75 buggy WHERE is kept only in the library and only because the CLI exposes it for operator before/after measurement — single source.

YAGNI: no buffer-hit ceiling, no PG version matrix, no Python version matrix, no `@pytest.mark` opt-in marker. Each is in scope to add later if data demands.

TDD: the new classifier field gets a unit test before the field exists (Task 1). The regression test is written against a known-good codebase (Task 4) so it passes initially — calibration in Task 5 narrows the scale rather than the test logic.
