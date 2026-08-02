# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Deciding what an attachment blob *is*, from its declared MIME type and the
original filenames it was received under.

Pure — no IO, no config object, no DB — so the extraction gate's decision can be
tested directly, which is the whole reason #216 went unnoticed: the rule looked
right at every call site while being fed a string that could never satisfy it.

**The extension must come from the original filename, never from
`attachment_blobs.path`.** That path is the content-addressable
`<root>/blobs/<aa>/<bb>/<sha256hex>` and carries no extension by construction,
so `Path(path).suffix` is always `""` — which silently reduced the
"MIME *or* extension" rule to "MIME only" and left every mis-typed attachment
unindexed. Original filenames live in `messages.attachments` JSONB.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

PDF_MIME_TYPE = "application/pdf"
PDF_EXTENSION = ".pdf"


def extension_of(filename: str | None) -> str:
    """Return `filename`'s lowercased extension including the dot, or `""`.

    `""` for a missing name, a name with no dot, a trailing dot, and a
    dotfile — `Path` treats a leading dot as part of the stem, which is right:
    `.bashrc` names no format.
    """
    return Path(filename).suffix.lower() if filename else ""


def is_allowlisted(
    mime_type: str | None,
    filenames: Iterable[str],
    *,
    mime_allowlist: Sequence[str],
    extension_allowlist: Sequence[str],
) -> bool:
    """True iff the blob's MIME type **or** any of its original filenames'
    extensions is allowlisted. Both comparisons are case-insensitive.

    Either match suffices because senders get this wrong in both directions: a
    real PDF arrives as `application/octet-stream` from mobile clients, and a
    `.dat` arrives labelled `text/plain`. `filenames` is plural because a blob
    is content-addressable and shared across every message carrying those bytes,
    each of which named it independently.
    """
    if (mime_type or "").lower() in {m.lower() for m in mime_allowlist}:
        return True
    allowed = {e.lower() for e in extension_allowlist}
    return any(extension_of(name) in allowed for name in filenames)


def preferred_filename(
    filenames: Sequence[str], extension_allowlist: Sequence[str]
) -> str | None:
    """Pick the single name to hand an extractor for format dispatch.

    Prefer one whose extension is allowlisted — that is the only thing the
    choice can affect, since dispatch reads nothing else off the name. Falls
    back to the first name so a blob admitted purely on its MIME type still has
    something to name in an error message; `None` only when the blob has no
    recorded filename at all.
    """
    allowed = {e.lower() for e in extension_allowlist}
    for name in filenames:
        if extension_of(name) in allowed:
            return name
    return filenames[0] if filenames else None


def is_pdf(mime_type: str | None, filename: str | None) -> bool:
    """True iff the blob is a PDF by MIME type or by filename extension.

    Gates the docling fallback, which is PDF-only.
    """
    return (
        (mime_type or "").lower() == PDF_MIME_TYPE
        or extension_of(filename) == PDF_EXTENSION
    )
