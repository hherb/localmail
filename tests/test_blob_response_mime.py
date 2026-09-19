# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The #32 MIME clamp, pinned once for both routes that share it."""
from __future__ import annotations

import pytest

from localmail.serve.routes.blob_response import _safe_response_mime


@pytest.mark.parametrize("stored", ["text/html", "Text/HTML", "IMAGE/SVG+XML"])
def test_a_risky_mime_is_clamped_whatever_its_case(stored: str) -> None:
    assert _safe_response_mime(stored) == "application/octet-stream"


@pytest.mark.parametrize("stored", [None, ""])
def test_a_missing_mime_is_served_as_octet_stream(stored: str | None) -> None:
    # attachment_blobs.mime_type is nullable; `.lower()` on None was a 500.
    assert _safe_response_mime(stored) == "application/octet-stream"


def test_a_safe_mime_passes_through_unchanged() -> None:
    assert _safe_response_mime("Application/PDF") == "Application/PDF"
