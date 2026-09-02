# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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

import psycopg

from tests.acceptance.browse_explain_lib import (
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
from tests.acceptance._harness_lock import harness_db_lock


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

    with harness_db_lock(args.dsn):
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
