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
  - `LinguaDetector`: wraps lingua-py, applies a confidence + length floor.
    Returns None for empty / short / low-confidence text so the caller can
    leave the column NULL ("unknown") rather than guess.
  - `make_detector(cfg)`: returns the configured detector, or None when
    `cfg.body_lang_enabled` is False.
  - `run_lang_detect_pass(conn, cfg, detector, ...)`: one batch over
    `messages WHERE body_lang IS NULL AND body_text IS NOT NULL`. Used by
    the embed worker every sweep and by the `lang-backfill` CLI in a loop.

Failure model mirrors `embed_worker.py`: per-message SAVEPOINT isolates
detector exceptions so a single poison body doesn't abort the batch; the
message is left NULL and skipped on subsequent sweeps until something
fixes it (different body text, updated detector).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import psycopg

from localmail.config import SearchConfig


log = logging.getLogger("localmail.search.lang_detect")


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
        stripped = text.strip() if text else ""
        if len(stripped) < self._min_text_chars:
            return None
        self._ensure_built()
        assert self._detector is not None
        # Lingua sees the stripped form so the length floor and the detector
        # input agree — otherwise leading/trailing whitespace would inflate
        # the apparent length above the floor.
        confidences = self._detector.compute_language_confidence_values(stripped)
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
) -> int:
    """Detect `body_lang` for one batch of pending messages.

    Selects up to `batch` (default `cfg.body_lang_detect_batch_size`)
    messages with NULL `body_lang` and non-NULL `body_text`, runs the
    detector on each body, and writes the result.

    Returns the number of rows whose `body_lang` transitioned from NULL to
    a non-NULL value in this call — *not* the number of rows visited. Rows
    the detector declined to label (too short, below confidence floor, or
    a poison exception) stay NULL and are not counted, so a `while pass:
    ...` backfill loop terminates once no row produced a new label in the
    current sweep. Pre-existing NULL rows can still be retried by a
    future call (e.g. after lowering `body_lang_min_confidence` or
    swapping the detector); termination here means "no further progress
    on this run," not "no rows are NULL."

    Per-message SAVEPOINT isolates detector exceptions: a single poison
    body lands a WARNING and stays NULL while the rest of the batch
    completes normally. There is no dedicated failure table — persistent
    failures resurface on every sweep via the WARNING log line, which
    matches the policy already in place for attachment chunking.
    """
    limit = batch if batch is not None else cfg.body_lang_detect_batch_size
    updated = 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, body_text FROM messages
            WHERE body_lang IS NULL
              AND body_text IS NOT NULL
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        )
        rows = cur.fetchall()
        if not rows:
            return 0
        for mid, body in rows:
            cur.execute("SAVEPOINT lang")
            try:
                code = detector.detect(body)
                if code is not None:
                    cur.execute(
                        "UPDATE messages SET body_lang = %s WHERE id = %s",
                        (code, mid),
                    )
                    updated += 1
                cur.execute("RELEASE SAVEPOINT lang")
            except Exception as exc:  # noqa: BLE001 — poison-pill isolation
                cur.execute("ROLLBACK TO SAVEPOINT lang")
                log.warning(
                    "lang detection failed for message %s: %s", mid, exc,
                )
    conn.commit()
    return updated
