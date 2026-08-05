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
    CLAIMABLE_WHERE_SQL,
    DECLINED_WHERE_SQL,
    RELABELABLE_WHERE_SQL,
    FixedDetector,
    LanguageDetector,
    LinguaDetector,
    make_detector,
    reopen_all,
    retry_declined,
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

    result = run_lang_detect_pass(db_conn, cfg, detector)

    assert result.labelled == 1
    assert _body_lang(db_conn, mid) == "de"


def test_run_lang_detect_pass_leaves_null_when_detector_returns_none(db_conn) -> None:
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, "ambiguous")
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({})

    result = run_lang_detect_pass(db_conn, cfg, detector)

    # The detector declined to label the row: body_lang stays NULL, so the row
    # is visited but not labelled. It is still stamped attempted, which is what
    # keeps it out of the next claim.
    assert result.visited == 1
    assert result.labelled == 0
    assert _body_lang(db_conn, mid) is None


def test_run_lang_detect_pass_skips_messages_with_existing_body_lang(db_conn) -> None:
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, "anything")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE messages SET body_lang = 'en' WHERE id = %s", (mid,))
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({"anything": "de"})

    result = run_lang_detect_pass(db_conn, cfg, detector)

    assert result.visited == 0
    # The pre-existing value must not be overwritten.
    assert _body_lang(db_conn, mid) == "en"


def test_run_lang_detect_pass_skips_messages_with_null_body_text(db_conn) -> None:
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, None)
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({})

    result = run_lang_detect_pass(db_conn, cfg, detector)

    assert result.visited == 0
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

    result = run_lang_detect_pass(db_conn, cfg, _PoisonDetector())

    # Only the labelled row is counted; the poison row stays NULL and is
    # excluded from the return count.
    assert result.labelled == 1
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

    assert first.labelled == 3
    assert second.labelled == 2
    assert third.visited == 0


def test_run_lang_detect_pass_explicit_batch_overrides_cfg(db_conn) -> None:
    acct = _seed_account(db_conn)
    for i in range(4):
        _seed_message(db_conn, acct, i, "anything")
    db_conn.commit()
    cfg = SearchConfig(body_lang_detect_batch_size=10)
    detector = FixedDetector({"anything": "en"})

    result = run_lang_detect_pass(db_conn, cfg, detector, batch=2)

    assert result.labelled == 2


def _attempted_at(conn, mid: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT body_lang_attempted_at FROM messages WHERE id = %s", (mid,)
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_advances_past_an_undetectable_head(db_conn) -> None:
    """#251: a full batch of unlabelable rows must not block the rows behind it.

    The claim is `ORDER BY id`, so before the fix a row the detector declined
    stayed NULL, kept satisfying the predicate, and was re-claimed in the same
    position on every sweep. With the first `batch_size` rows unlabelable the
    head of the queue was permanently occupied and detection stopped
    archive-wide — 7744 labelled against 100020 pending on the live Mac
    archive, frozen for weeks.
    """
    acct = _seed_account(db_conn)
    for i in range(3):
        _seed_message(db_conn, acct, i, f"junk {i}")       # never labelable
    labelable = [_seed_message(db_conn, acct, 10 + i, f"real {i}") for i in range(3)]
    db_conn.commit()
    cfg = SearchConfig(body_lang_detect_batch_size=3)
    detector = FixedDetector({f"real {i}": "en" for i in range(3)})

    first = run_lang_detect_pass(db_conn, cfg, detector)
    second = run_lang_detect_pass(db_conn, cfg, detector)

    # Pass 1 consumes the unlabelable head and labels nothing...
    assert first.visited == 3
    assert first.labelled == 0
    # ...and pass 2 reaches the rows behind it. Before the fix this was 0.
    assert second.visited == 3
    assert second.labelled == 3
    assert [_body_lang(db_conn, mid) for mid in labelable] == ["en", "en", "en"]


def test_declined_rows_are_stamped_attempted(db_conn) -> None:
    """A declined row records that the detector ran, so the claim skips it.

    `body_lang` itself cannot carry this — NULL has to keep meaning "unknown"
    for the `lang:` filter — which is why the state lives in its own column
    rather than in a sentinel language value (#216's one-way door).
    """
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, "undetectable")
    db_conn.commit()

    run_lang_detect_pass(db_conn, SearchConfig(), FixedDetector({}))

    assert _body_lang(db_conn, mid) is None
    assert _attempted_at(db_conn, mid) is not None


def test_labelled_rows_are_stamped_attempted(db_conn) -> None:
    """Labelling and declining share one write, so neither can skip the stamp."""
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, "anything")
    db_conn.commit()

    run_lang_detect_pass(db_conn, SearchConfig(), FixedDetector({"anything": "en"}))

    assert _body_lang(db_conn, mid) == "en"
    assert _attempted_at(db_conn, mid) is not None


def test_poison_rows_are_stamped_attempted_and_not_reclaimed(db_conn) -> None:
    """A body that makes the detector raise must not re-wedge the head.

    The poison branch rolls back to the savepoint, which would discard the
    stamp along with everything else — so the stamp is rewritten afterwards
    under its own nested savepoint. Without that, a reliably-crashing body
    starves the queue exactly as a declined one did.
    """
    acct = _seed_account(db_conn)
    poison = _seed_message(db_conn, acct, 1, "bad body")
    good = _seed_message(db_conn, acct, 2, "good body")
    db_conn.commit()

    class _PoisonDetector:
        def detect(self, text: str) -> str | None:
            if "bad" in text:
                raise RuntimeError("boom")
            return "en"

    first = run_lang_detect_pass(db_conn, SearchConfig(), _PoisonDetector())
    second = run_lang_detect_pass(db_conn, SearchConfig(), _PoisonDetector())

    assert first.visited == 2
    assert first.labelled == 1
    assert _body_lang(db_conn, poison) is None
    assert _attempted_at(db_conn, poison) is not None
    assert _body_lang(db_conn, good) == "en"
    # The poison row is not claimed a second time.
    assert second.visited == 0


def test_pass_reports_visited_and_labelled_separately(db_conn) -> None:
    """`visited` and `labelled` answer different questions (#251).

    A batch in which the detector declines every row makes real progress —
    each row is stamped attempted and will not be claimed again — while
    labelling nothing. Conflating the two is what let the drain loops read
    "declined everything" as "queue drained" and wedged detection
    archive-wide.
    """
    acct = _seed_account(db_conn)
    _seed_message(db_conn, acct, 1, "labelable")
    _seed_message(db_conn, acct, 2, "not labelable")
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({"labelable": "en"})

    result = run_lang_detect_pass(db_conn, cfg, detector)

    assert result.visited == 2
    assert result.labelled == 1


# ---------------------------------------------------------------------------
# retry_declined + the claim/declined predicate partition
# ---------------------------------------------------------------------------


def test_retry_declined_reopens_declined_rows(db_conn) -> None:
    """Lowering a threshold must be able to re-open what the old one turned away.

    This is the escape hatch #216's `type-skipped` sentinel lacks: widening
    that allowlist silently does not re-open the blobs it was widened for.
    """
    acct = _seed_account(db_conn)
    mid = _seed_message(db_conn, acct, 1, "was undetectable")
    db_conn.commit()
    run_lang_detect_pass(db_conn, SearchConfig(), FixedDetector({}))
    assert _attempted_at(db_conn, mid) is not None

    reopened = retry_declined(db_conn)

    assert reopened == 1
    assert _attempted_at(db_conn, mid) is None
    # A looser detector now reaches it.
    result = run_lang_detect_pass(
        db_conn, SearchConfig(), FixedDetector({"was undetectable": "en"})
    )
    assert result.labelled == 1
    assert _body_lang(db_conn, mid) == "en"


def test_retry_declined_leaves_labelled_rows_alone(db_conn) -> None:
    """Re-opening declines must not re-run detection over the whole archive."""
    acct = _seed_account(db_conn)
    labelled = _seed_message(db_conn, acct, 1, "anything")
    declined = _seed_message(db_conn, acct, 2, "undetectable")
    db_conn.commit()
    run_lang_detect_pass(db_conn, SearchConfig(), FixedDetector({"anything": "en"}))

    reopened = retry_declined(db_conn)

    assert reopened == 1
    assert _body_lang(db_conn, labelled) == "en"
    assert _attempted_at(db_conn, labelled) is not None
    assert _attempted_at(db_conn, declined) is None


def test_claimable_and_declined_predicates_partition_the_pending_set(db_conn) -> None:
    """The two predicates must be disjoint and jointly exhaustive.

    `search-status` reports one count from each and calls the pair a complete
    picture of NULL-body_lang rows. An overlap would double-count; a gap would
    hide rows from the operator entirely — which is precisely how #251 went
    unnoticed while 100020 rows sat unreachable.
    """
    acct = _seed_account(db_conn)
    _seed_message(db_conn, acct, 1, "anything")       # will be labelled
    _seed_message(db_conn, acct, 2, "undetectable")   # will be declined
    _seed_message(db_conn, acct, 3, None)             # no body: in neither set
    db_conn.commit()
    run_lang_detect_pass(
        db_conn, SearchConfig(body_lang_detect_batch_size=1),
        FixedDetector({"anything": "en"}),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM messages WHERE ({CLAIMABLE_WHERE_SQL})"
            f" AND ({DECLINED_WHERE_SQL})"
        )
        overlap = cur.fetchone()
        cur.execute(
            f"SELECT count(*) FROM messages"
            f" WHERE body_lang IS NULL AND body_text IS NOT NULL"
            f" AND NOT ({CLAIMABLE_WHERE_SQL}) AND NOT ({DECLINED_WHERE_SQL})"
        )
        gap = cur.fetchone()
    assert overlap is not None and overlap[0] == 0
    assert gap is not None and gap[0] == 0


def test_run_lang_detect_pass_loop_terminates_on_persistent_null(db_conn) -> None:
    """A drain loop must terminate when no pending row can be labelled.

    Termination comes from `visited` reaching zero — the rows were claimed
    once, stamped attempted, and are gone from the claim. Reading termination
    off the *labelled* count instead is what traded a spinning loop for a
    starving one (#251): it stopped after a batch that had made no progress
    at all, leaving everything behind that batch unreachable forever.
    """
    acct = _seed_account(db_conn)
    for i in range(3):
        _seed_message(db_conn, acct, i, "x")  # below the 20-char floor
    db_conn.commit()
    cfg = SearchConfig()
    detector = FixedDetector({})  # never labels anything

    first = run_lang_detect_pass(db_conn, cfg, detector)
    second = run_lang_detect_pass(db_conn, cfg, detector)

    assert first.visited == 3
    assert first.labelled == 0
    assert second.visited == 0
    assert second.labelled == 0


class _StubLingua:
    """Stands in for a built lingua detector; records what it was asked."""

    def __init__(self, code: str = "YO", confidence: float = 0.99) -> None:
        self.seen: list[str] = []
        self._code = code
        self._confidence = confidence

    def compute_language_confidence_values(self, text: str):  # noqa: ANN202
        self.seen.append(text)
        iso = type("Iso", (), {"name": self._code})
        lang = type("Lang", (), {"iso_code_639_1": iso})
        return [type("Val", (), {"value": self._confidence, "language": lang})()]


def _detector_with(stub: _StubLingua) -> LinguaDetector:
    det = LinguaDetector(min_confidence=0.65, min_text_chars=20)
    det._detector = stub
    return det


def test_detector_sees_the_body_with_urls_stripped() -> None:
    """The tracking URL never reaches lingua (#255)."""
    stub = _StubLingua()
    det = _detector_with(stub)
    det.detect("Last chance to save https://ct.klclick.com/f/a/IgDYzk3AXlDh~~/AASl5QA today")
    assert stub.seen == ["Last chance to save today"]


def test_url_only_body_is_declined_without_consulting_the_detector() -> None:
    """A body of pure tracking URLs has no linguistic content.

    Measured raw it clears the 20-char floor and earns a confident wrong label;
    measured after normalisation it is empty. The floor must therefore apply to
    the normalised text, and the detector must not be consulted at all.
    """
    stub = _StubLingua()
    det = _detector_with(stub)
    assert det.detect("https://a.example/aaaaaaaaaaaaaaaaaaaa http://b.example/bbbb") is None
    assert stub.seen == []


def test_length_floor_applies_to_the_normalised_text() -> None:
    """Long enough raw, too short once the URL is gone."""
    stub = _StubLingua()
    det = _detector_with(stub)
    assert det.detect("Hi https://example.com/a-very-long-tracking-path-here") is None
    assert stub.seen == []


def test_full_accuracy_is_the_default() -> None:
    """Low-accuracy mode measured worse on every axis (#255).

    On the live Mac archive it left 300/300 implausibly-labelled rows wrong
    where full accuracy left 3, while costing *more* resident memory (239 MB
    vs 227 MB) and running 2.3x slower. The knob survives for a
    memory-constrained host; the default must not.
    """
    assert SearchConfig().body_lang_low_accuracy is False
    detector = make_detector(SearchConfig())
    assert isinstance(detector, LinguaDetector)
    assert detector._low_accuracy is False


def test_reopen_all_clears_labels_and_attempt_stamps(db_conn) -> None:
    """Re-labelling must reach rows that already carry a (wrong) label.

    `retry_declined` cannot: by construction it only re-opens rows with no
    label, and the #255 defect is rows labelled confidently and wrongly.
    """
    acct = _seed_account(db_conn)
    _seed_message(db_conn, acct, 1, "anything")      # will be labelled
    _seed_message(db_conn, acct, 2, "undetectable")  # will be declined
    _seed_message(db_conn, acct, 3, None)            # no body
    db_conn.commit()
    run_lang_detect_pass(db_conn, SearchConfig(), FixedDetector({"anything": "en"}))

    assert reopen_all(db_conn) == 2  # the bodied rows only

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM messages"
            " WHERE body_lang IS NOT NULL OR body_lang_attempted_at IS NOT NULL"
        )
        row = cur.fetchone()
    assert row is not None and row[0] == 0


def test_relabelable_contains_claimable_and_declined(db_conn) -> None:
    """One authority per predicate; claimable and declined are subsets.

    `search-status` reads the first two and the relabel path reads the third.
    A drift between them is how #251 stayed invisible for weeks.
    """
    acct = _seed_account(db_conn)
    _seed_message(db_conn, acct, 1, "anything")
    _seed_message(db_conn, acct, 2, "undetectable")
    _seed_message(db_conn, acct, 3, None)
    db_conn.commit()
    run_lang_detect_pass(db_conn, SearchConfig(), FixedDetector({"anything": "en"}))

    with db_conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM messages"
            f" WHERE ({CLAIMABLE_WHERE_SQL}) AND NOT ({RELABELABLE_WHERE_SQL})"
        )
        claimable_outside = cur.fetchone()
        cur.execute(
            f"SELECT count(*) FROM messages"
            f" WHERE ({DECLINED_WHERE_SQL}) AND NOT ({RELABELABLE_WHERE_SQL})"
        )
        declined_outside = cur.fetchone()
    assert claimable_outside is not None and claimable_outside[0] == 0
    assert declined_outside is not None and declined_outside[0] == 0


def test_constructor_default_matches_the_config_default() -> None:
    """One meaning for "which lingua mode", not two that can drift.

    `make_detector` always passes the config value, so a contradicting
    constructor default is only reachable by a direct construction — which is
    exactly the silent-wrong-default footgun #234 removed elsewhere.
    """
    direct = LinguaDetector(min_confidence=0.65, min_text_chars=20)
    assert direct._low_accuracy is SearchConfig().body_lang_low_accuracy
