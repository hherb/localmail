# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Classifying an attachment blob by declared MIME type + original filename.

The rules these pin are what #216 was about: the extraction gate used to read
the extension off `attachment_blobs.path`, which is the content-addressable
`<root>/blobs/<aa>/<bb>/<sha256hex>` and **has no extension by construction**.
Every extension comparison was therefore against `""`, so the whole
`extractor_extension_allowlist` was dead and a mis-typed attachment was
unindexable. The extension has to come from the *original* per-message
filename, which is what these functions take.
"""
from __future__ import annotations

import pytest

from localmail.search.attachment_kind import (
    extension_of,
    is_allowlisted,
    is_pdf,
    preferred_filename,
)

MIMES = ["application/pdf", "text/plain"]
EXTS = [".pdf", ".docx", ".txt"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.pdf", ".pdf"),
        ("REPORT.PDF", ".pdf"),
        ("archive.tar.gz", ".gz"),
        ("no-extension", ""),
        ("trailing.", ""),
        (".bashrc", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_extension_of(filename: str | None, expected: str) -> None:
    assert extension_of(filename) == expected


def test_the_blob_path_has_no_extension_to_find() -> None:
    """The #216 bug in one assertion: this is the string the gate used to read,
    and it can never match an allowlist entry."""
    blob_path = "/var/localmail/blobs/ab/cd/" + "ab" * 32
    assert extension_of(blob_path) == ""


def test_an_allowlisted_mime_type_is_enough() -> None:
    assert is_allowlisted(
        "application/pdf", [], mime_allowlist=MIMES, extension_allowlist=EXTS
    )


def test_an_allowlisted_extension_is_enough() -> None:
    """The case #216 reported: mail clients routinely send a real PDF as
    `application/octet-stream`."""
    assert is_allowlisted(
        "application/octet-stream",
        ["invoice.pdf"],
        mime_allowlist=MIMES,
        extension_allowlist=EXTS,
    )


def test_any_of_the_blobs_filenames_may_carry_the_extension() -> None:
    """A blob is shared across messages and can hold several original names —
    one recognisable name is enough to make the bytes worth extracting."""
    assert is_allowlisted(
        "application/octet-stream",
        ["attachment.bin", "contract.docx"],
        mime_allowlist=MIMES,
        extension_allowlist=EXTS,
    )


def test_neither_mime_nor_extension_means_not_allowlisted() -> None:
    assert not is_allowlisted(
        "image/png", ["logo.png"], mime_allowlist=MIMES, extension_allowlist=EXTS
    )


def test_a_blob_with_no_known_filename_falls_back_to_its_mime_type() -> None:
    assert not is_allowlisted(
        "image/png", [], mime_allowlist=MIMES, extension_allowlist=EXTS
    )
    assert is_allowlisted(
        "text/plain", [], mime_allowlist=MIMES, extension_allowlist=EXTS
    )


def test_a_missing_mime_type_is_not_a_crash() -> None:
    """`attachment_blobs.mime_type` is nullable."""
    assert not is_allowlisted(
        None, [], mime_allowlist=MIMES, extension_allowlist=EXTS
    )
    assert is_allowlisted(
        None, ["notes.txt"], mime_allowlist=MIMES, extension_allowlist=EXTS
    )


@pytest.mark.parametrize("case", ["application/PDF", "APPLICATION/pdf"])
def test_mime_comparison_is_case_insensitive(case: str) -> None:
    assert is_allowlisted(
        case, [], mime_allowlist=MIMES, extension_allowlist=EXTS
    )


def test_preferred_filename_picks_one_the_extractors_can_dispatch_on() -> None:
    assert preferred_filename(["blob.bin", "deck.pptx", "notes.txt"], EXTS) == "notes.txt"


def test_preferred_filename_falls_back_to_the_first_name() -> None:
    """Nothing recognisable, but the extractor still wants *a* name for its
    dispatch attempt and its error message."""
    assert preferred_filename(["blob.bin", "other.zip"], EXTS) == "blob.bin"


def test_preferred_filename_of_nothing_is_none() -> None:
    assert preferred_filename([], EXTS) is None


def test_is_pdf_by_mime_or_by_filename() -> None:
    assert is_pdf("application/pdf", None)
    assert is_pdf("application/octet-stream", "scan.PDF")
    assert not is_pdf("image/png", "logo.png")
    assert not is_pdf(None, None)
