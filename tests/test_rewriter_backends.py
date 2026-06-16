"""Tests for the pluggable rewriter backends + factory."""
from __future__ import annotations

from datetime import date as _date

import httpx
import pytest

from localmail.config import SearchConfig


def test_ollama_backcompat_import_path_still_works():
    # External callers may import the deep path; PEP 562 __getattr__ keeps it.
    from localmail.search.rewriter import OllamaLLMRewriter as FromRewriter
    from localmail.search.rewriter_backends import OllamaLLMRewriter as FromBackends

    assert FromRewriter is FromBackends


def test_package_level_export_still_works():
    from localmail.search import OllamaLLMRewriter  # noqa: F401
