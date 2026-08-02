# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for :mod:`localmail.ocr_policy` (#248).

Pure: these run without docling installed. The module turns the operator's
``search.extractor_ocr_engine`` string into a decision the DoclingExtractor
can act on, and renders the message for an engine docling does not know.
"""

from __future__ import annotations

import pytest

from localmail.ocr_policy import (
    OCR_AUTO,
    OCR_DISABLED,
    OcrPlan,
    plan_ocr,
    unknown_engine_message,
)


class TestPlanOcr:
    """``plan_ocr`` maps the config string to (do_ocr, engine_kind)."""

    def test_auto_is_the_default_and_enables_ocr(self) -> None:
        """docling's OcrAutoOptions probes ocrmac/rapidocr/easyocr and, when
        none is installed, logs a warning and passes pages through — no raise.
        That graceful degradation is exactly what #248 needed, and hardcoding
        EasyOcrOptions was what overrode it."""
        assert plan_ocr(OCR_AUTO) == OcrPlan(do_ocr=True, engine_kind="auto")

    def test_none_disables_ocr_entirely(self) -> None:
        """Our own value — docling has no 'off' kind. For an operator who has
        an engine installed but does not want to pay OCR on a large archive."""
        assert plan_ocr(OCR_DISABLED) == OcrPlan(do_ocr=False, engine_kind=None)

    def test_a_named_engine_is_passed_through_as_the_docling_kind(self) -> None:
        """The config value IS docling's registry key, so there is no mapping
        table here to drift against a docling upgrade."""
        assert plan_ocr("ocrmac") == OcrPlan(do_ocr=True, engine_kind="ocrmac")

    @pytest.mark.parametrize("raw", ["  auto  ", "AUTO", "Auto\t"])
    def test_case_and_surrounding_whitespace_are_forgiven(self, raw: str) -> None:
        """A hand-edited TOML value should not fail over a stray capital."""
        assert plan_ocr(raw) == OcrPlan(do_ocr=True, engine_kind="auto")

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_an_empty_value_disables_ocr_rather_than_asking_docling(
        self, raw: str
    ) -> None:
        """An empty string is not a docling kind; treating it as 'none' beats
        forwarding it and getting an unknown-engine error for a blank."""
        assert plan_ocr(raw) == OcrPlan(do_ocr=False, engine_kind=None)

    def test_an_unknown_name_is_still_forwarded(self) -> None:
        """Validation belongs against docling's live registry, not a literal
        list here that would go stale on a docling upgrade."""
        assert plan_ocr("nosuch") == OcrPlan(do_ocr=True, engine_kind="nosuch")

    def test_the_plan_is_frozen(self) -> None:
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            plan_ocr(OCR_AUTO).do_ocr = False  # type: ignore[misc]


class TestUnknownEngineMessage:
    """The operator-facing text for an engine docling does not register."""

    def test_names_the_offending_value(self) -> None:
        assert "'easyocrr'" in unknown_engine_message("easyocrr", ["auto", "easyocr"])

    def test_lists_what_docling_does_know(self) -> None:
        msg = unknown_engine_message("x", ["auto", "easyocr", "ocrmac"])
        assert "auto, easyocr, ocrmac" in msg

    def test_names_the_config_key_so_the_operator_can_find_it(self) -> None:
        assert "extractor_ocr_engine" in unknown_engine_message("x", ["auto"])

    def test_mentions_the_disable_value(self) -> None:
        """``none`` is ours, so it never appears in docling's registry — the
        operator would otherwise have no way to discover it from the error."""
        assert OCR_DISABLED in unknown_engine_message("x", ["auto"])

    def test_survives_an_empty_registry(self) -> None:
        """A docling build that registers nothing must still produce a
        readable sentence, not a dangling 'docling knows: '."""
        msg = unknown_engine_message("x", [])
        assert "'x'" in msg and msg.strip().endswith(".")
