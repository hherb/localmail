"""Tests for the extractor protocol and LightweightExtractor skeleton.

Per-format extraction is added in Tasks 7-10; this file currently only
asserts the type contracts and the .supports() allowlist behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localmail.search.extractor import (
    AttachmentExtractor,
    ExtractedText,
    ExtractorError,
    LightweightExtractor,
)


def test_extracted_text_is_frozen_dataclass() -> None:
    et = ExtractedText(text="hello", page_count=1, extractor="x@1")
    with pytest.raises(Exception):
        et.text = "world"  # type: ignore[misc]


def test_lightweight_supports_pdf_mime_and_ext() -> None:
    lw = LightweightExtractor()
    assert lw.supports("application/pdf", "foo.pdf")
    assert lw.supports(None, "foo.pdf")
    assert lw.supports("application/pdf", "")
    assert not lw.supports("video/mp4", "foo.mp4")


def test_lightweight_does_not_support_image() -> None:
    lw = LightweightExtractor()
    assert not lw.supports("image/png", "logo.png")


def test_extractor_protocol_runtime_checkable() -> None:
    """AttachmentExtractor is @runtime_checkable so workers can verify
    a configured class implements the interface."""
    lw = LightweightExtractor()
    assert isinstance(lw, AttachmentExtractor)
