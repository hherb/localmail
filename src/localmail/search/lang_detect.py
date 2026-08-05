# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Per-message language detection.

Populates `messages.body_lang` (ISO 639-1 lowercase) so the `lang:` search
DSL token and `/v1/search?languages=` API filter return rows. Without this
pass `messages.body_lang IS NULL` for every row and any `lang:` query
returns zero hits (the searcher emits a one-shot WARNING in that case).

Layout:

  - `LanguageDetector` protocol: anything with `detect(text) -> str | None`.
  - `FixedDetector`: deterministic in-memory map for tests.
  - `LinguaDetector`: wraps lingua-py. Normalises the body through
    `lang_text.normalize_for_detection` (URLs out), then applies a confidence
    + length floor to the *normalised* text. Returns None for empty / short /
    low-confidence text so the caller can leave the column NULL ("unknown")
    rather than guess.
  - `make_detector(cfg)`: returns the configured detector, or None when
    `cfg.body_lang_enabled` is False.
  - `run_lang_detect_pass(conn, cfg, detector, ...)`: one batch over
    `CLAIMABLE_WHERE_SQL`. Used by the embed worker every sweep and by the
    `lang-backfill` CLI in a loop.
  - `retry_declined(conn)`: re-open rows a previous policy declined.

Failure model mirrors `embed_worker.py`: per-message SAVEPOINT isolates
detector exceptions so a single poison body doesn't abort the batch.

**Every claimed row is stamped `body_lang_attempted_at`, labelled or not.**
`body_lang` must keep meaning "detected language, else unknown" for the
`lang:` filter, so it cannot also record "we tried". Without a separate
record a declined row stayed in the claim predicate and — under the stable
`ORDER BY id` — was re-selected in the same position forever, starving every
message behind it (#251). Migration 0035 adds the column; the two module
constants below are the one authority for its predicates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import psycopg

from localmail.config import SearchConfig
from localmail.search.lang_text import normalize_for_detection


log = logging.getLogger("localmail.search.lang_detect")


#: Rows the detector has not been run against yet — the lang-detect work queue.
#: The claim query, `search-status`'s `body_lang_pending` count, and migration
#: 0035's `messages_body_lang_claimable_idx` predicate must all agree on this.
#: A drift makes `search-status` report work the worker will never claim (or
#: hide work it will), which is how #251 stayed invisible for weeks.
CLAIMABLE_WHERE_SQL = (
    "body_lang IS NULL "
    "AND body_text IS NOT NULL "
    "AND body_lang_attempted_at IS NULL"
)

#: Rows the detector has run on and declined to label. A non-empty set is
#: normal — separator blocks, bare URLs, bodies under `body_lang_min_text_chars`
#: — and is what `search-status` reports as `body_lang_declined`. Together with
#: `CLAIMABLE_WHERE_SQL` this partitions every NULL-body_lang row that has a
#: body; `tests/test_lang_detect.py` pins that partition.
DECLINED_WHERE_SQL = (
    "body_lang IS NULL "
    "AND body_text IS NOT NULL "
    "AND body_lang_attempted_at IS NOT NULL"
)


@dataclass(frozen=True)
class LangDetectPass:
    """Outcome of one `run_lang_detect_pass` call.

    `visited` and `labelled` answer different questions, and conflating them
    is what wedged detection archive-wide (#251): a batch in which the
    detector declines every row makes real progress — each row is stamped
    attempted and will not be claimed again — while labelling nothing.

    Drain loops must terminate on `visited == 0`; progress reporting wants
    `labelled`. There is deliberately no `__bool__`: `if not result:` reads
    ambiguously, and an implicit reading of this exact value is the bug.
    """

    visited: int
    labelled: int


@runtime_checkable
class LanguageDetector(Protocol):
    """Detect ISO 639-1 lowercase language code for `text`, or None.

    Returning None must mean "unknown": text is empty, too short to be
    reliable, or the detector's confidence is below the configured floor.
    Callers store NULL in `messages.body_lang` in that case.
    """

    def detect(self, text: str) -> str | None: ...


class FixedDetector:
    """In-memory `text -> lang` mapping; used as a test seam.

    Any text not present in the mapping yields None ("unknown").
    """

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._mapping = mapping

    def detect(self, text: str) -> str | None:
        return self._mapping.get(text)


class LinguaDetector:
    """Lingua-py detector with confidence + length floors.

    Loads lingua lazily so this module is importable in environments where
    `lingua-language-detector` is not installed (e.g. trimmed Docker images,
    test CI without the optional ML stack). The first `detect()` call
    triggers the package import and the detector build.
    """

    def __init__(
        self,
        *,
        min_confidence: float,
        min_text_chars: int,
        low_accuracy: bool = True,
    ) -> None:
        self._min_confidence = min_confidence
        self._min_text_chars = min_text_chars
        self._low_accuracy = low_accuracy
        # `Any` because lingua is an optional runtime dep we don't import at
        # module load; the type-checker can't see the concrete class.
        self._detector: Any = None

    def _ensure_built(self) -> None:
        if self._detector is not None:
            return
        from lingua import LanguageDetectorBuilder  # noqa: PLC0415
        builder = LanguageDetectorBuilder.from_all_languages()
        if self._low_accuracy:
            builder = builder.with_low_accuracy_mode()
        self._detector = builder.build()

    def detect(self, text: str) -> str | None:
        normalized = normalize_for_detection(text) if text else ""
        # The floor measures the *normalised* text. A body of pure tracking
        # URLs is long enough raw to clear it and earns a confident wrong
        # label; normalised it is empty and correctly declines (#255).
        if len(normalized) < self._min_text_chars:
            return None
        self._ensure_built()
        assert self._detector is not None
        confidences = self._detector.compute_language_confidence_values(normalized)
        if not confidences:
            return None
        top = confidences[0]
        if top.value < self._min_confidence:
            return None
        return top.language.iso_code_639_1.name.lower()


def make_detector(cfg: SearchConfig) -> LanguageDetector | None:
    """Return the detector configured by `cfg`, or None when disabled.

    Centralising construction keeps the daemon, CLI, and tests aligned on
    the same defaults. Callers that want a different policy (e.g. a fake
    detector for tests) construct an instance directly.
    """
    if not cfg.body_lang_enabled:
        return None
    return LinguaDetector(
        min_confidence=cfg.body_lang_min_confidence,
        min_text_chars=cfg.body_lang_min_text_chars,
        low_accuracy=cfg.body_lang_low_accuracy,
    )


def run_lang_detect_pass(
    conn: psycopg.Connection,
    cfg: SearchConfig,
    detector: LanguageDetector,
    *,
    batch: int | None = None,
) -> LangDetectPass:
    """Detect `body_lang` for one batch of pending messages.

    Selects up to `batch` (default `cfg.body_lang_detect_batch_size`) rows
    matching `CLAIMABLE_WHERE_SQL`, runs the detector on each body, and writes
    the result. Every claimed row is stamped `body_lang_attempted_at`, whether
    or not it gained a label, so it leaves the queue either way.

    Returns a `LangDetectPass`. Drain loops terminate on `visited == 0` —
    "nothing left to claim" — never on `labelled == 0`, which only means the
    detector declined this batch and says nothing about what lies behind it.
    That distinction is the whole of #251: reading termination off the label
    count stopped the loops on a batch of unlabelable rows and left every
    message after them permanently undetected.

    Rows the detector declined can be re-opened later — after lowering
    `body_lang_min_confidence` or swapping the detector — via
    `retry_declined` / `localmail lang-backfill --retry-declined`.

    Per-message SAVEPOINT isolates detector exceptions: a single poison body
    lands a WARNING and stays NULL while the rest of the batch completes
    normally. It is still stamped attempted (see `_mark_attempted_safely`),
    because a body that crashes the detector once will crash it every time.
    There is no dedicated failure table — persistent failures resurface on
    every sweep via the WARNING log line, matching the policy already in place
    for attachment chunking.
    """
    limit = batch if batch is not None else cfg.body_lang_detect_batch_size
    labelled = 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, body_text FROM messages
            WHERE {CLAIMABLE_WHERE_SQL}
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,  # noqa: S608 — module constant, no caller input
            (limit,),
        )
        rows = cur.fetchall()
        if not rows:
            return LangDetectPass(visited=0, labelled=0)
        for mid, body in rows:
            cur.execute("SAVEPOINT lang")
            try:
                code = detector.detect(body)
                # One write for both outcomes: `code` is NULL when the detector
                # declined. Labelling and declining cannot diverge, so no future
                # branch can label a row without stamping it.
                cur.execute(
                    "UPDATE messages"
                    " SET body_lang = %s, body_lang_attempted_at = now()"
                    " WHERE id = %s",
                    (code, mid),
                )
                cur.execute("RELEASE SAVEPOINT lang")
                if code is not None:
                    labelled += 1
            except Exception as exc:  # noqa: BLE001 — poison-pill isolation
                cur.execute("ROLLBACK TO SAVEPOINT lang")
                log.warning(
                    "lang detection failed for message %s: %s", mid, exc,
                )
                _mark_attempted_safely(cur, mid)
    conn.commit()
    return LangDetectPass(visited=len(rows), labelled=labelled)


def _mark_attempted_safely(cur: psycopg.Cursor, mid: int) -> None:
    """Stamp `body_lang_attempted_at` for a row whose detection raised.

    The poison branch has just rolled back to its savepoint, which discarded
    the stamp along with everything else. Without rewriting it here, a body
    that reliably crashes the detector re-enters the claim on every sweep and
    starves the queue exactly as a declined body did before #251.

    The SAVEPOINT statement sits *outside* the try — like
    `sync.record_failed_message` and `fetch_retry.record_attempt` — so
    `ROLLBACK TO` is always valid even if issuing the savepoint is itself what
    failed. A failure to stamp leaves the row claimable, which is the safe
    direction: re-attempting costs a sweep, whereas a lost stamp would be
    invisible.
    """
    cur.execute("SAVEPOINT lang_attempt")
    try:
        cur.execute(
            "UPDATE messages SET body_lang_attempted_at = now() WHERE id = %s",
            (mid,),
        )
        cur.execute("RELEASE SAVEPOINT lang_attempt")
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not kill the batch
        cur.execute("ROLLBACK TO SAVEPOINT lang_attempt")
        log.warning(
            "could not record lang attempt for message %s: %s", mid, exc,
        )


def retry_declined(conn: psycopg.Connection) -> int:
    """Re-open every row the detector ran on and declined; return the count.

    The escape hatch that makes `body_lang_attempted_at` strictly better than
    a sentinel language value: after lowering `body_lang_min_confidence` or
    `body_lang_min_text_chars`, or swapping the detector, the rows the old
    policy turned away become claimable again. #216's `type-skipped` sentinel
    has no equivalent — widening that allowlist silently does not re-open the
    blobs it was widened for.

    Rows that already carry a `body_lang` are untouched: this re-opens
    declines, it does not re-run detection over the archive.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE messages SET body_lang_attempted_at = NULL"
            f" WHERE {DECLINED_WHERE_SQL}"  # noqa: S608 — module constant
        )
        reopened = cur.rowcount
    conn.commit()
    return reopened
