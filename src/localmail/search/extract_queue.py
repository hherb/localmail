# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one authority on what the attachment-extraction queue still holds.

`extract_worker._claim_batch` decides which blobs the worker will process;
`localmail search-status` reports how many are outstanding. Those two used to
be written apart, and drifted (#277): the report derived pending work as
``eligible - extracted``, where *extracted* meant "has an ``attachment_text``
row with non-empty text". Every blob the worker had already **disposed of by
writing an empty-text sentinel** — ``type-skipped`` (#216),
``lightweight-empty``, ``size-skipped``, a #266-healed row — therefore counted
as outstanding forever, because the claim excludes a blob the moment *any*
``attachment_text`` row exists. Blobs parked at a retry cap (#153) were the
same defect wearing a different row.

That is exactly the drift #251 found on the language half of the same command,
and the fix has the same shape: the claim predicate lives here, and both the
worker and the report compose it rather than restating it.

The eligible population divides into four **disjoint and jointly exhaustive**
buckets — `EXTRACTED_WHERE_SQL`, `NO_TEXT_WHERE_SQL`, `GAVE_UP_WHERE_SQL` and
`CLAIMABLE_WHERE_SQL`, each documented at its own definition below, and pinned
as a partition by `test_extract_queue.py`. Only the last is work; ``gave_up``
clears via ``localmail retry-failed-extractions``, and ``no_text`` is terminal
by design (``_claim_batch`` never re-opens a rowed blob — see the one-way-door
note on ``SKIPPED_EXTRACTOR``).

**Those four count only allowlisted blobs, and the claim does not.** The worker
claims every un-rowed blob under both retry caps and *then* applies the
allowlist in Python, disposing of a miss with a ``type-skipped`` row (#216). So
the partition is the allowlisted *subset* of the worker's queue, and
`CLAIMABLE_TOTAL_SQL` reports the whole of it — without which
``blobs_pending == 0`` would read as "nothing left to do" on precisely the
archive #216 was filed about (16,542 blobs, 0/20 allowlisted in the next
claim).

Every fragment below assumes ``attachment_blobs`` is aliased ``b`` and that
`QUEUE_FROM_SQL` supplied the joins, so the aliases the predicates read are
defined by the same string that introduces them. `ALLOWLISTED_WHERE_SQL` reads
one more, ``ext``, which only `QUEUE_COUNTS_FROM_SQL` introduces — the join
behind it reads every message, and the worker's claim must not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from types import MappingProxyType

import psycopg
from psycopg.rows import class_row

from localmail.config import SearchConfig

_UNREPORTED_FIELDS = frozenset({"misfiled"})
"""`QueueCounts` fields that are self-checks rather than operator counters."""


class QueueCountsInconsistent(ValueError):
    """The four buckets failed to account for the eligible population.

    Named so `cli.search_status` can catch exactly this and report it to the
    operator, rather than over-catching every `ValueError` the DB layer might
    raise. Reaching it means a predicate or the schema changed — see
    `QueueCounts.__post_init__` for the constraints it stands on.
    """


QUEUE_FROM_SQL = """
    FROM attachment_blobs b
    LEFT JOIN attachment_text       t  USING (sha256)
    LEFT JOIN failed_extractions    f  USING (sha256)
    LEFT JOIN transient_extractions tr USING (sha256)
"""
"""FROM clause defining the aliases every predicate here reads.

All three joined tables key on ``sha256 PRIMARY KEY``, so no join can multiply
a blob into two rows and inflate the counters.

Shared with `extract_worker._claim_batch`, which runs it every sweep under
``FOR UPDATE … SKIP LOCKED`` — so it stays three primary-key lookups per blob.
The eligibility lookup, which reads every message, hangs off
`QUEUE_COUNTS_FROM_SQL` instead.
"""

EXTENSION_MATCH_JOIN_SQL = """
    LEFT JOIN (
        SELECT DISTINCT a->>'sha256' AS sha256_hex
          FROM messages m, jsonb_array_elements(m.attachments) AS a
         WHERE lower(substring(a->>'filename' FROM '.(\\.[^.]+)$'))
               = ANY(%(extension_allowlist)s)
    ) ext ON ext.sha256_hex = encode(b.sha256, 'hex')
"""
"""Which blobs some message named with an allowlisted extension — resolved
once for the whole archive, not once per blob (#280).

Written as a correlated ``EXISTS`` this was a ``SubPlan``, and correlating the
operand on ``b.sha256`` is exactly what makes Postgres abandon
``messages_attachments_gin``: the containment predicate costs ~42 with a
constant operand and ~36,203 as a per-blob ``Seq Scan on messages``, which on
the 127k-message Mac archive measured **13:04** for one counter. As a
decorrelated join the same answer takes ~70 ms.

**A ``LEFT JOIN`` rather than an uncorrelated ``IN (SELECT …)``**, which reads
more simply and plans the same way *until* the hashed subplan outgrows
``work_mem`` — at which point Postgres silently reverts to re-executing it per
row and the fix is undone on precisely the large archives it was written for. A
hash join spills to disk instead.

``DISTINCT`` is load-bearing: a blob is content-addressable and global, so
every message carrying those bytes names it independently (#216), and without
it a blob several messages named admissibly would fan out into one row per
message — inflating ``eligible`` past the blob count and breaking the
partition. It also matches `attachment_kind.is_allowlisted`'s "**any** one of
its filenames" rule, which asks whether a match exists, not how many.
"""

QUEUE_COUNTS_FROM_SQL = QUEUE_FROM_SQL + EXTENSION_MATCH_JOIN_SQL
"""`QUEUE_FROM_SQL` plus the alias `ALLOWLISTED_WHERE_SQL` reads."""

_UNDER_RETRY_CAPS_SQL = (
    "(f.sha256 IS NULL OR f.retry_count < %(max_retries)s)"
    " AND (tr.sha256 IS NULL OR tr.transient_count < %(max_transient_retries)s)"
)
"""Both retry budgets still have room: the poison-pill counter
(``failed_extractions.retry_count``) and the independent consecutive-transient
counter (``transient_extractions.transient_count``, #153).

``retry_count`` and ``transient_count`` are both ``NOT NULL``, so this is never
SQL ``NULL`` and `GAVE_UP_WHERE_SQL` is a genuine complement of it rather than
a third truth value that would drop rows out of every bucket.

**Private**: on its own it is only half a claim. Composed without the
``t.sha256 IS NULL`` half it silently admits already-processed blobs, which is
the drift this module exists to end — take `CLAIMABLE_WHERE_SQL` instead.
"""

CLAIMABLE_WHERE_SQL = f"t.sha256 IS NULL AND ({_UNDER_RETRY_CAPS_SQL})"
"""What `extract_worker._claim_batch` will hand the worker.

Deliberately carries **no allowlist half** — the MIME/extension allowlists live
in `SearchConfig` and are applied in Python after the claim (#216), so a blob
outside them is claimed, then disposed of with a ``type-skipped`` row.
"""

GAVE_UP_WHERE_SQL = f"t.sha256 IS NULL AND NOT ({_UNDER_RETRY_CAPS_SQL})"
"""Unprocessed, but parked: a retry cap is exhausted so the claim skips it.

Recoverable, but only half of it is *listable*: the transient counter (#153)
writes no ``failed_extractions`` row, so ``localmail list-failed-extractions``
shows the poison-pill half only. ``retry-failed-extractions`` clears both.
"""

EXTRACTED_WHERE_SQL = "t.sha256 IS NOT NULL AND t.extracted_text <> ''"
"""Processed, with text to show for it."""

NO_TEXT_WHERE_SQL = "t.sha256 IS NOT NULL AND t.extracted_text = ''"
"""Processed into a sentinel: skipped by size or type, extracted to nothing,
or healed by #266. ``attachment_text.extracted_text`` is ``NOT NULL``, so this
and `EXTRACTED_WHERE_SQL` between them cover every rowed blob."""

BUCKET_WHERE_SQL: Mapping[str, str] = MappingProxyType(
    {
        "extracted": EXTRACTED_WHERE_SQL,
        "no_text": NO_TEXT_WHERE_SQL,
        "gave_up": GAVE_UP_WHERE_SQL,
        "pending": CLAIMABLE_WHERE_SQL,
    }
)
"""The partition, keyed by the `QueueCounts` field each bucket fills.

The one authority for *what the buckets are*: `QUEUE_COUNTS_SQL`'s aggregates,
the misfiled check that guards their disjointness, and
`QueueCounts.__post_init__`'s sum are all derived from it, so a fifth
disposition cannot reach one and miss another. Adding a key without adding the
matching field fails loudly at `class_row` construction.
"""

ALLOWLISTED_WHERE_SQL = """
    lower(b.mime_type) = ANY(%(mime_allowlist)s)
    OR ext.sha256_hex IS NOT NULL
"""
"""SQL mirror of `attachment_kind.is_allowlisted`, pinned by a differential
test over the same inputs (`test_the_sql_allowlist_agrees_with_the_python_one`).

The extension half reads the **original filename** out of
``messages.attachments``, never ``attachment_blobs.path`` — that path is
content-addressable and extensionless, so a ``suffix`` comparison against it is
always ``''`` (#216). It arrives here as the `EXTENSION_MATCH_JOIN_SQL` alias
rather than as a subquery, so this predicate requires `QUEUE_COUNTS_FROM_SQL`,
not `QUEUE_FROM_SQL`. Unparenthesised; combine it as ``WHERE (…)``.

Two details exist only to keep the mirror exact, and both were divergences
before #277's review found them. Both now live on the join that resolves the
extension half, `EXTENSION_MATCH_JOIN_SQL`:

* **Both sides of each comparison are lowercased.** `is_allowlisted` lowers the
  stored value *and* the configured one, so a ``config.toml`` carrying
  ``"Application/PDF"`` or ``".PDF"`` matched in Python and never in SQL —
  dropping a whole class of blob out of every bucket while the partition still
  summed, i.e. #277's failure mode wearing a different hat. The config half is
  lowered in `allowlist_params`, so these fragments can assume it.
* **The leading character before the extension is required.** ``Path(".txt")``
  has no suffix — a dotfile names no format (`attachment_kind.extension_of`) —
  but ``'\\.[^.]+$'`` matched the whole of ``.txt``. The ``.`` prefix demands
  one character ahead of the final dot, which reproduces Python on every input
  the differential test covers, including ``..txt`` and ``archive.tar.gz``.
"""


def bucket_count_sql(buckets: Mapping[str, str]) -> str:
    """One ``count(*) FILTER (…) AS <bucket>`` aggregate per bucket."""
    return ",\n           ".join(
        f"count(*) FILTER (WHERE {predicate}) AS {name}"
        for name, predicate in buckets.items()
    )


def misfiled_count_sql(buckets: Mapping[str, str]) -> str:
    """One aggregate counting rows that land in other than exactly one bucket.

    The sum check in `QueueCounts.__post_init__` is implied by a partition but
    does not imply one: a row counted twice plus a row counted not at all adds
    up correctly (#284). Casting each predicate to ``int`` and demanding the
    total be exactly ``1`` catches overlap and gap in the same expression.

    ``IS DISTINCT FROM`` rather than ``<>`` because the total is SQL ``NULL``
    as soon as any predicate is — which is what a migration relaxing one of the
    ``NOT NULL`` columns the predicates pivot on would produce, and a ``NULL``
    filter condition counts nothing and reports the archive as healthy.

    Takes the buckets as a parameter so the detector can be exercised against
    contrived predicates: the production ones are structurally incapable of
    overlapping, which is exactly why nothing tested this guard.
    """
    total = "\n         + ".join(f"({predicate})::int" for predicate in buckets.values())
    return f"count(*) FILTER (WHERE ({total}) IS DISTINCT FROM 1) AS misfiled"


QUEUE_COUNTS_SQL = f"""
    SELECT count(*) AS eligible,
           {bucket_count_sql(BUCKET_WHERE_SQL)},
           {misfiled_count_sql(BUCKET_WHERE_SQL)}
    {QUEUE_COUNTS_FROM_SQL}
    WHERE ({ALLOWLISTED_WHERE_SQL})
"""  # noqa: S608 — every fragment is a module constant; runtime values bind as %(name)s
"""The partition, in one pass.

Aggregated rather than run as one statement per bucket because the allowlist
half reads every message — still the dominant cost of `search-status` even
decorrelated (#280), and there is no reason to pay for it more than once.

**Do not split this into separate statements.** One statement is one snapshot
under READ COMMITTED, which is what lets `QueueCounts.__post_init__` treat a
gap as a predicate bug: run five queries and an extract worker committing
between them would drop a blob out of every bucket, turning a read-only status
command into an intermittent crash.
"""

CLAIMABLE_TOTAL_SQL = f"""
    SELECT count(*) AS claimable
    {QUEUE_FROM_SQL}
    WHERE {CLAIMABLE_WHERE_SQL}
"""  # noqa: S608 — every fragment is a module constant; runtime values bind as %(name)s
"""Everything `_claim_batch` will hand the worker, allowlist and all.

Its own statement because it must *not* carry `ALLOWLISTED_WHERE_SQL`, and it
composes `QUEUE_FROM_SQL` rather than `QUEUE_COUNTS_FROM_SQL` so it never
touches `messages` at all: three primary-key lookups per blob, no subquery.
"""


def cap_params(*, max_retries: int, max_transient_retries: int) -> dict[str, int]:
    """Bind the two retry budgets `CLAIMABLE_WHERE_SQL` reads.

    Keyword-only: the two are both small ints with the same type, so a
    positional call site could swap them and still run.
    """
    return {
        "max_retries": max_retries,
        "max_transient_retries": max_transient_retries,
    }


def allowlist_params(
    *, mime_allowlist: Sequence[str], extension_allowlist: Sequence[str]
) -> dict[str, list[str]]:
    """Bind the two allowlists `ALLOWLISTED_WHERE_SQL` reads.

    Lowercased here so the SQL can compare against an already-folded operand,
    matching `attachment_kind.is_allowlisted`, which lowers both sides.
    """
    return {
        "mime_allowlist": [m.lower() for m in mime_allowlist],
        "extension_allowlist": [e.lower() for e in extension_allowlist],
    }


@dataclass(frozen=True, slots=True)
class QueueCounts:
    """How the eligible blob population divides, at one moment.

    Every field but `claimable` is built by name from `QUEUE_COUNTS_SQL`'s
    column aliases (psycopg ``class_row``), so the aliases — not their order —
    are what bind to them.
    """

    eligible: int
    extracted: int
    no_text: int
    gave_up: int
    pending: int
    claimable: int = 0
    """Un-rowed blobs under both retry caps, **ignoring the allowlist** — the
    worker's true queue depth, of which `pending` is the allowlisted subset.

    Defaulted because it comes from `CLAIMABLE_TOTAL_SQL`, a second statement
    that `class_row` does not see; `fetch_queue_counts` fills it in. It is
    deliberately excluded from the partition check: a different statement is a
    different snapshot, so `claimable < pending` is briefly possible while a
    worker commits, and asserting otherwise would crash the command over a race
    that resolves itself.
    """

    misfiled: int = 0
    """Eligible blobs that `misfiled_count_sql` found in other than exactly one
    bucket — always ``0`` on an instance that exists, since `__post_init__`
    refuses any other value. Not reported: see `status_field_names`."""

    def __post_init__(self) -> None:
        if self.misfiled:
            raise QueueCountsInconsistent(
                f"{self.misfiled} of {self.eligible} eligible blobs are misfiled "
                f"— each must match exactly one of "
                f"{', '.join(BUCKET_WHERE_SQL)}; a predicate now overlaps, has a "
                f"gap, or reads a column that has become nullable"
            )
        total = sum(getattr(self, name) for name in BUCKET_WHERE_SQL)
        if total != self.eligible:
            raise QueueCountsInconsistent(
                f"extraction queue buckets do not sum to the eligible "
                f"population: extracted={self.extracted} no_text={self.no_text} "
                f"gave_up={self.gave_up} pending={self.pending} "
                f"(total {total}) vs eligible={self.eligible}"
            )

    def __bool__(self) -> bool:
        """Refuse the implicit read that caused #251 and #259.

        ``if counts:`` would be silently always-True and looks exactly like a
        drained-queue check; ask for `pending` or `claimable` by name.
        """
        raise TypeError(
            "QueueCounts has no truth value — test .pending or .claimable"
        )

    @staticmethod
    def status_field_names() -> tuple[str, ...]:
        """The `search-status` payload keys these counts occupy.

        Derived from the fields rather than typed out, so a bucket added here
        cannot go missing from the command that exists to report it — the
        hand-copied projection is the last place #277's drift could hide.

        `misfiled` is the one exclusion: it is a self-check whose only
        non-zero value raises, so reporting it would put a permanently-``0``
        line in front of an operator and invite the wrong question.
        """
        return tuple(
            f"blobs_{f.name}"
            for f in fields(QueueCounts)
            if f.name not in _UNREPORTED_FIELDS
        )

    def status_fields(self) -> dict[str, int]:
        """This instance as that payload slice."""
        return {
            f"blobs_{f.name}": getattr(self, f.name)
            for f in fields(self)
            if f.name not in _UNREPORTED_FIELDS
        }


def fetch_queue_counts(conn: psycopg.Connection, cfg: SearchConfig) -> QueueCounts:
    """Count the blob population by disposition (two queries).

    The only IO in this module — kept beside the predicates it runs so a
    reader sees the rule and its single caller together, the same shape as
    `uids.max_label_uid`.
    """
    caps = cap_params(
        max_retries=cfg.extract_worker_max_retries,
        max_transient_retries=cfg.extract_worker_max_transient_retries,
    )
    params: dict[str, object] = {
        **caps,
        **allowlist_params(
            mime_allowlist=cfg.extractor_mime_allowlist,
            extension_allowlist=cfg.extractor_extension_allowlist,
        ),
    }
    with conn.cursor(row_factory=class_row(QueueCounts)) as cur:
        cur.execute(QUEUE_COUNTS_SQL, params)
        partition = cur.fetchone()
        assert partition is not None
    with conn.cursor() as cur:
        cur.execute(CLAIMABLE_TOTAL_SQL, caps)
        row = cur.fetchone()
        assert row is not None
    return replace(partition, claimable=row[0])
