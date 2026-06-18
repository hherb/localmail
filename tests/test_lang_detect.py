# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the body-language detection module.

The module ships with a `LanguageDetector` protocol, a deterministic
`FixedDetector` for unit tests, a `LinguaDetector` that wraps lingua-py,
and a `run_lang_detect_pass(conn, cfg, detector, ...)` function that walks
`messages WHERE body_lang IS NULL` and updates the column.

DB-backed tests use the existing `db_conn` fixture; the lingua wrapper
tests skip cleanly if the package is missing so the suite still passes in
trimmed environments.
"""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.lang_detect import (
    FixedDetector,
    LanguageDetector,
    LinguaDetector,
    make_detector,
    run_lang_detect_pass,
)


# Body-text fixtures: short enough to keep tests fast, long enough to clear
# the configured min_text_chars floor (20 chars in the default config).
DE_BODY = (
    "Wir treffen uns nächste Woche zur Konferenz in Berlin. "
    "Bitte bringe das Programm mit."
)
EN_BODY = (
    "Looking forward to the conference in Berlin next week. "
    "Please bring the agenda."
)
ES_BODY = (
    "Nos vemos la próxima semana en la conferencia de Berlín. "
    "Trae el programa por favor."
)


# ---------------------------------------------------------------------------
# FixedDetector — used as a test seam in the rest of the suite
# ---------------------------------------------------------------------------


def test_fixed_detector_returns_mapped_lang() -> None:
    detector = FixedDetector({"hello": "en", "hola": "es"})
    assert detector.detect("hello") == "en"
    assert detector.detect("hola") == "es"


def test_fixed_detector_returns_none_for_unmapped_text() -> None:
    detector = FixedDetector({"hello": "en"})
    assert detector.detect("unknown") is None


def test_fixed_detector_satisfies_protocol() -> None:
    detector: LanguageDetector = FixedDetector({})
    assert detector.detect("anything") is None


# ---------------------------------------------------------------------------
# LinguaDetector — guarded behind importorskip so trimmed envs still pass
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lingua_detector() -> LinguaDetector:
    pytest.importorskip("lingua")
    return LinguaDetector(min_confidence=0.65, min_text_chars=20, low_accuracy=True)


def test_lingua_detector_returns_iso_639_1_lowercase_de(lingua_detector: LinguaDetector) -> None:
    assert lingua_detector.detect(DE_BODY) == "de"


def test_lingua_detector_returns_iso_639_1_lowercase_en(lingua_detector: LinguaDetector) -> None:
    assert lingua_detector.detect(EN_BODY) == "en"


def test_lingua_detector_returns_iso_639_1_lowercase_es(lingua_detector: LinguaDetector) -> None:
    assert lingua_detector.detect(ES_BODY) == "es"


def test_lingua_detector_returns_none_for_empty_text(lingua_detector: LinguaDetector) -> None:
    assert lingua_detector.detect("") is None


def test_lingua_detector_returns_none_for_whitespace_only(lingua_detector: LinguaDetector) -> None:
    assert lingua_detector.detect("   \n  \t ") is None


def test_lingua_detector_returns_none_for_short_text(lingua_detector: LinguaDetector) -> None:
    # 5 chars; well below the 20-char floor in the fixture.
    assert lingua_detector.detect("hello") is None


def test_lingua_detector_respects_high_confidence_threshold() -> None:
    # Threshold = 1.01 is unreachable (probabilities sum to 1.0), so even an
    # unambiguous text must return None.
    pytest.importorskip("lingua")
    strict = LinguaDetector(min_confidence=1.01, min_text_chars=20, low_accuracy=True)
    assert strict.detect(EN_BODY) is None


# ---------------------------------------------------------------------------
# make_detector — config switch
# ---------------------------------------------------------------------------


def test_make_detector_returns_none_when_disabled() -> None:
    cfg = SearchConfig(body_lang_enabled=False)
    assert make_detector(cfg) is None


def test_make_detector_returns_lingua_when_enabled() -> None:
    pytest.importorskip("lingua")
    cfg = SearchConfig(body_lang_enabled=True)
    detector = make_detector(cfg)
    assert isinstance(detector, LinguaDetector)


# ---------------------------------------------------------------------------
# run_lang_detect_pass — DB-backed
# ---------------------------------------------------------------------------


def _seed_account(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a', 'a@x', 'h', 'password') RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _seed_message(conn, acct_id: int, idx: int, body: str | None) -> int:
    sha = bytes([idx % 256]) * 32
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " from_addr, body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s) RETURNING id",
            (acct_id, f"<m{idx}@x>", sha, f"subj{idx}", "x@y",
             body, b"raw", len(body or "")),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _body_lang(conn, mid: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT body_lang FROM messages WHERE id = %s", (mid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_run_lang_detect_pass_updates_body_lang(db_conn) -> None:
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, "anything")
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({"anything": "de"})

    processed = run_lang_detect_pass(db_conn, cfg, detector)

    assert processed == 1
    assert _body_lang(db_conn, mid) == "de"


def test_run_lang_detect_pass_leaves_null_when_detector_returns_none(db_conn) -> None:
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, "ambiguous")
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({})

    processed = run_lang_detect_pass(db_conn, cfg, detector)

    # The detector declined to label the row; body_lang stays NULL and the
    # row is not counted. The return value tracks rows whose body_lang
    # transitioned from NULL to non-NULL — this guarantees the
    # `lang-backfill` CLI loop terminates on archives full of bodies the
    # detector cannot label, rather than re-claiming the same rows forever.
    assert processed == 0
    assert _body_lang(db_conn, mid) is None


def test_run_lang_detect_pass_skips_messages_with_existing_body_lang(db_conn) -> None:
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, "anything")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE messages SET body_lang = 'en' WHERE id = %s", (mid,))
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({"anything": "de"})

    processed = run_lang_detect_pass(db_conn, cfg, detector)

    assert processed == 0
    # The pre-existing value must not be overwritten.
    assert _body_lang(db_conn, mid) == "en"


def test_run_lang_detect_pass_skips_messages_with_null_body_text(db_conn) -> None:
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, None)
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({})

    processed = run_lang_detect_pass(db_conn, cfg, detector)

    assert processed == 0
    assert _body_lang(db_conn, mid) is None


def test_run_lang_detect_pass_isolates_poison_message(db_conn) -> None:
    """A detector that raises on one message must not abort the batch."""
    acct = _seed_account(db_conn)
    poison = _seed_message(db_conn, acct, 1, "bad body")
    good = _seed_message(db_conn, acct, 2, "good body")
    db_conn.commit()
    cfg = SearchConfig()

    class _PoisonDetector:
        def detect(self, text: str) -> str | None:
            if "bad" in text:
                raise RuntimeError("boom")
            return "en"

    processed = run_lang_detect_pass(db_conn, cfg, _PoisonDetector())

    # Only the labelled row is counted; the poison row stays NULL and is
    # excluded from the return count.
    assert processed == 1
    assert _body_lang(db_conn, poison) is None
    assert _body_lang(db_conn, good) == "en"


def test_run_lang_detect_pass_respects_batch_size(db_conn) -> None:
    acct = _seed_account(db_conn)
    for i in range(5):
        _seed_message(db_conn, acct, i, f"body number {i} with enough text to pass the floor")
    db_conn.commit()
    cfg = SearchConfig(body_lang_detect_batch_size=3)
    detector = FixedDetector(
        {f"body number {i} with enough text to pass the floor": "en" for i in range(5)}
    )

    first = run_lang_detect_pass(db_conn, cfg, detector)
    second = run_lang_detect_pass(db_conn, cfg, detector)
    third = run_lang_detect_pass(db_conn, cfg, detector)

    assert first == 3
    assert second == 2
    assert third == 0


def test_run_lang_detect_pass_explicit_batch_overrides_cfg(db_conn) -> None:
    acct = _seed_account(db_conn)
    for i in range(4):
        _seed_message(db_conn, acct, i, "anything")
    db_conn.commit()
    cfg = SearchConfig(body_lang_detect_batch_size=10)
    detector = FixedDetector({"anything": "en"})

    processed = run_lang_detect_pass(db_conn, cfg, detector, batch=2)

    assert processed == 2


def test_run_lang_detect_pass_loop_terminates_on_persistent_null(db_conn) -> None:
    """Regression: a while-loop draining the function must terminate even when
    the detector cannot label any of the pending rows. Prior behaviour returned
    the claimed-row count, which made the lang-backfill / embed-backfill CLI
    loops re-claim the same NULL rows forever on archives full of short bodies.
    """
    acct = _seed_account(db_conn)
    for i in range(3):
        _seed_message(db_conn, acct, i, "x")  # below the 20-char floor
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({})  # never labels anything

    first = run_lang_detect_pass(db_conn, cfg, detector)
    second = run_lang_detect_pass(db_conn, cfg, detector)

    assert first == 0
    assert second == 0
