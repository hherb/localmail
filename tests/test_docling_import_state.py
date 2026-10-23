# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A docling that is present but cannot be imported is held, not written off (#405).

``_try_import_docling`` used to catch every ``ImportError`` and answer "not
installed". A partial ``uv sync`` that dropped one of docling's dependencies
therefore read exactly like an install without the ``[extraction]`` extra, and
the worker stored a ``lightweight-empty`` sentinel for every scanned PDF.
``_claim_batch`` never re-claims a blob that has an ``attachment_text`` row, so
repairing the install did not bring those PDFs back: the one-way door #400
closed for converter construction, one step earlier.

The two states are told apart by ``importlib.util.find_spec("docling")``,
never by reading an exception message. These tests fake both halves so they
mean the same thing whether or not docling is installed here (CI installs no
``[extraction]``):

- presence: ``importlib.util.find_spec`` is patched;
- import failure: ``sys.modules["docling.document_converter"]`` is set, which
  ``import`` consults before it looks at the parent package.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

import localmail.search.extractor as ext_mod
from localmail.config import SearchConfig
from localmail.search.extract_worker import run_extract_worker_once
from localmail.search.extractor import (
    ExtractorConfigurationError,
    _try_import_docling,
)
from tests.test_extract_worker import _seed_blob

_CONVERTER_MODULE = "docling.document_converter"


def _docling_present(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    real = importlib.util.find_spec

    def fake(name: str, package: str | None = None):
        if name == "docling":
            return object() if present else None
        return real(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake)


class _RaisingModule(types.ModuleType):
    """A module whose every missing attribute raises ``exc`` — a native
    library that fails as the name is resolved."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(_CONVERTER_MODULE)
        self._exc = exc

    def __getattr__(self, name: str):
        raise self._exc


def _break_docling_import(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException | None = None
) -> None:
    """Make ``from docling.document_converter import …`` fail.

    ``None`` in ``sys.modules`` raises ``ModuleNotFoundError`` for the
    submodule, the shape a partial sync leaves behind; a ``_RaisingModule``
    raises ``exc`` instead.
    """
    monkeypatch.setitem(
        sys.modules,
        _CONVERTER_MODULE,
        None if exc is None else _RaisingModule(exc),
    )


def test_absent_docling_still_reads_as_not_installed(monkeypatch) -> None:
    """The documented behaviour without the ``[extraction]`` extra is kept,
    and the import is not even attempted."""
    _docling_present(monkeypatch, False)
    _break_docling_import(monkeypatch, AssertionError("import was attempted"))

    assert _try_import_docling() is None


@pytest.mark.parametrize(
    "exc",
    [None, OSError("libtorch_cpu.dylib: image not found")],
    ids=["import-error", "os-error"],
)
def test_a_present_docling_that_cannot_be_imported_is_a_configuration_error(
    exc, monkeypatch
) -> None:
    """An ``ImportError`` *and* a non-``ImportError`` (a native library) both
    become an ``ExtractorConfigurationError``. The ``OSError`` used to escape
    to the worker's outer safety net as an ``'unexpected'`` poison pill."""
    _docling_present(monkeypatch, True)
    _break_docling_import(monkeypatch, exc)

    with pytest.raises(ExtractorConfigurationError) as raised:
        _try_import_docling()

    expected = ModuleNotFoundError if exc is None else type(exc)
    assert isinstance(raised.value.__cause__, expected)
    assert "installed but cannot be imported" in str(raised.value)


def test_an_unresolvable_spec_is_held_too(monkeypatch) -> None:
    """``find_spec`` itself can raise (a broken finder, or a ``docling`` in
    ``sys.modules`` with no ``__spec__``). That is not proof of absence, so it
    fails closed with the rest."""

    def boom(name: str, package: str | None = None):
        raise ValueError("docling.__spec__ is None")

    monkeypatch.setattr(importlib.util, "find_spec", boom)

    with pytest.raises(ExtractorConfigurationError):
        _try_import_docling()


def _empty_lightweight(monkeypatch) -> None:
    monkeypatch.setattr(
        ext_mod.LightweightExtractor,
        "extract",
        lambda self, p, m, filename=None: ext_mod.ExtractedText(
            text="", page_count=0, extractor="lightweight@1.0"
        ),
    )


def _blob_state(db_conn, sha: bytes) -> tuple[object, int, int]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor FROM attachment_text WHERE sha256 = %s", (sha,)
        )
        stored = cur.fetchone()
        cur.execute(
            "SELECT transient_count FROM transient_extractions WHERE sha256 = %s",
            (sha,),
        )
        transient = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM failed_extractions WHERE sha256 = %s", (sha,)
        )
        failed = cur.fetchone()
    assert failed is not None
    return (
        None if stored is None else stored[0],
        0 if transient is None else transient[0],
        failed[0],
    )


def test_a_scanned_pdf_is_held_while_docling_cannot_be_imported(
    db_conn, tmp_path: Path, monkeypatch
) -> None:
    """The acceptance: no ``attachment_text`` row (the blob stays claimable),
    one tick on the #153 transient budget, and no poison pill."""
    _docling_present(monkeypatch, True)
    _break_docling_import(monkeypatch)
    _empty_lightweight(monkeypatch)
    sha = _seed_blob(db_conn, b"%PDF-1.4 scan", "application/pdf", tmp_path, "s.pdf")

    run_extract_worker_once(db_conn, SearchConfig())

    assert _blob_state(db_conn, sha) == (None, 1, 0)


def test_a_scanned_pdf_without_docling_still_gets_the_sentinel(
    db_conn, tmp_path: Path, monkeypatch
) -> None:
    """Positive control: an install without the extra keeps writing
    ``lightweight-empty``, so the fix cannot be a blanket hold."""
    _docling_present(monkeypatch, False)
    _empty_lightweight(monkeypatch)
    sha = _seed_blob(db_conn, b"%PDF-1.4 scan", "application/pdf", tmp_path, "s.pdf")

    run_extract_worker_once(db_conn, SearchConfig())

    assert _blob_state(db_conn, sha) == ("lightweight-empty", 0, 0)


def test_a_broken_docling_does_not_hold_blobs_it_would_never_read(
    db_conn, tmp_path: Path, monkeypatch
) -> None:
    """docling only ever sees PDFs, so an empty non-PDF is unaffected by its
    state: it gets the sentinel, not a hold."""
    _docling_present(monkeypatch, True)
    _break_docling_import(monkeypatch)
    sha = _seed_blob(db_conn, b"", "text/plain", tmp_path, "empty.txt")

    run_extract_worker_once(db_conn, SearchConfig())

    assert _blob_state(db_conn, sha) == ("lightweight-empty", 0, 0)
