# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`localmail.version_report` — resolving our own version, and saying so.

`__version__` degrades to a sentinel when the distribution metadata cannot be
read, and that fallback has to stay: import must not fail, and `/v1/version`
emitting `server_version: null` breaks the GUI's connect probe, which decodes
the field as a non-optional String.

What #291 fixes is that the sentinel was never *surfaced*. `localmail
--version` answered `0.0.0+unknown` with exit 0 and nothing on stderr — "the
version could not be determined", in a format indistinguishable from a
successful answer, at the one moment an operator is diagnosing a broken
install.

The two causes are kept apart because they have **different remedies**, which
is the whole reason an operator reads this line: nothing is installed (run an
install) versus the dist-info is present but has no `Version:` header (a
damaged install — reinstalling *over* it is what helps). They used to collapse
to the same string.
"""
from __future__ import annotations

import importlib.metadata

import pytest

from localmail.version_report import (
    UNKNOWN_VERSION,
    ResolvedVersion,
    VersionSource,
    resolve_version,
    unknown_version_diagnostic,
)


@pytest.fixture
def metadata_version(monkeypatch: pytest.MonkeyPatch):
    """Stub `importlib.metadata.version` for the duration of one test.

    `resolve_version` must reach through `importlib.metadata` at *call* time
    (not bind the name at import) for this to work — which is also what lets
    `localmail/__init__.py`'s reload-based tests keep observing the derivation.
    """

    def _install(fake) -> None:
        monkeypatch.setattr(importlib.metadata, "version", fake)

    return _install


def test_readable_metadata_resolves_to_the_installed_version(metadata_version) -> None:
    metadata_version(lambda _name: "1.2.3+sentinel")
    assert resolve_version() == ResolvedVersion("1.2.3+sentinel", VersionSource.INSTALLED)


def test_absent_distribution_resolves_to_the_sentinel(metadata_version) -> None:
    """A source tree that was never installed — `python -m localmail` from a
    checkout, which is a first-class entry point (the 2B.4 supervisor launches
    the daemon that way)."""

    def _raise(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    metadata_version(_raise)
    assert resolve_version() == ResolvedVersion(
        UNKNOWN_VERSION, VersionSource.NOT_INSTALLED
    )


@pytest.mark.parametrize("empty", [None, ""])
def test_version_less_metadata_is_a_separate_cause(metadata_version, empty) -> None:
    """`version()` *returns* None — it does not raise — when a dist-info exists
    but its METADATA carries no `Version:` header. typeshed declares it `-> str`,
    so mypy cannot catch it; an empty string is the same damage one layer on.

    Distinct from `NOT_INSTALLED` because the remedy differs: there is something
    installed here, and it needs replacing rather than adding.
    """
    metadata_version(lambda _name: empty)
    assert resolve_version() == ResolvedVersion(
        UNKNOWN_VERSION, VersionSource.METADATA_INCOMPLETE
    )


def test_a_known_version_carries_no_diagnostic() -> None:
    """The overwhelmingly common case. A warning here would train operators to
    ignore the line that matters."""
    assert unknown_version_diagnostic(VersionSource.INSTALLED) is None


def test_each_unknown_cause_names_a_distinct_remedy() -> None:
    """The point of splitting the causes: `uv sync` does not repair a dist-info
    that is already present, and `--reinstall` is wasted on a tree that has
    none. If both branches said the same thing, the split would be decoration.
    """
    never = unknown_version_diagnostic(VersionSource.NOT_INSTALLED)
    damaged = unknown_version_diagnostic(VersionSource.METADATA_INCOMPLETE)
    assert never is not None and damaged is not None
    assert never != damaged
    assert "uv tool install localmail" in never
    assert "--reinstall" in damaged
    assert "--reinstall" not in never


def test_every_unknown_source_has_a_diagnostic() -> None:
    """Exhaustiveness, for the reason `BUCKET_WHERE_SQL` is one authority: a
    fourth cause added to the enum without a message would silently return
    `None` — i.e. reinstate exactly the #291 defect for that cause, with the
    flag looking healthy.
    """
    unmapped = [
        source
        for source in VersionSource
        if source is not VersionSource.INSTALLED
        and unknown_version_diagnostic(source) is None
    ]
    assert unmapped == []


def test_the_sentinel_is_named_not_repeated() -> None:
    """Both unknown branches report the *same* string, and it is the one every
    other reader compares against. #291's first scope item: the literal was
    written out twice in `__init__.py` and quoted a third time in a comment."""
    assert UNKNOWN_VERSION == "0.0.0+unknown"
