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

The causes are kept apart because they have **different remedies**, which is the
whole reason an operator reads this line: nothing is installed (run an install)
versus the dist-info is present but has no `Version:` header (a damaged install
— reinstalling *over* it is what helps) versus the metadata could not be read
at all (#296 — the file or the filesystem under it is broken). They used to
collapse to the same string.
"""
from __future__ import annotations

import importlib.metadata
from enum import Enum

import pytest

from localmail.version_report import (
    UNKNOWN_VERSION,
    ResolvedVersion,
    VersionSource,
    resolve_version,
    unknown_version_diagnostic,
)
from localmail.version_report import _CAUSE_PREFIX


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
    """The sources importable without their distribution metadata.

    Not `python -m localmail` from a bare checkout — the src layout makes that
    a `ModuleNotFoundError` before this branch is reached, and the 2B.4
    supervisor runs `sys.executable -m localmail` against an interpreter where
    the package *is* installed. The reachable shapes are `PYTHONPATH=src`, a
    vendored copy of the tree, and a dist-info removed by a partial sync.
    """

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


#: The two shapes #296 reproduced. A `METADATA` written in another encoding (or
#: truncated mid-multibyte) decodes to `UnicodeDecodeError`; a failing or
#: network-mounted `site-packages` gives a bare `OSError`. Neither is in
#: `PathDistribution.read_text`'s `suppress(...)` list, so both propagate.
_UNREADABLE_METADATA_ERRORS = [
    UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte"),
    OSError(5, "Input/output error"),
]


@pytest.mark.parametrize(
    "exc", _UNREADABLE_METADATA_ERRORS, ids=lambda e: type(e).__name__
)
def test_an_unreadable_metadata_file_is_a_third_cause(metadata_version, exc) -> None:
    """#296: reading the metadata can *fail*, not just come back empty.

    `resolve_version` used to guard only `PackageNotFoundError`, so anything
    else propagated straight out of `import localmail` and killed every entry
    point — including `--version`, the one command whose whole purpose is
    diagnosing a broken install. Reproduced end-to-end with a latin-1 byte in a
    `localmail-9.9.9.dist-info/METADATA` placed ahead on `sys.path`.

    Distinct from `METADATA_INCOMPLETE` because the damage is different: there
    the file was read and had no `Version:`; here it could not be read at all,
    which a filesystem fault can cause without the install being wrong.
    """

    def _raise(_name: str) -> str:
        raise exc

    metadata_version(_raise)
    resolved = resolve_version()
    assert resolved.version == UNKNOWN_VERSION
    assert resolved.source is VersionSource.METADATA_UNREADABLE
    assert resolved.detail is not None
    # The type name always leads, so `str(exc)` alone (empty for much of what
    # fails here) cannot satisfy this.
    assert resolved.detail.startswith(type(exc).__name__)


def test_the_unreadable_cause_reports_which_exception_it_swallowed() -> None:
    """Broadening the catch to `Exception` is only defensible if the exception
    is *reported* rather than discarded — a `UnicodeDecodeError` and an EIO on a
    network mount need different investigations, and the remedy text cannot tell
    them apart.
    """
    rendered = unknown_version_diagnostic(
        VersionSource.METADATA_UNREADABLE, detail="UnicodeDecodeError"
    )
    assert rendered is not None
    assert "UnicodeDecodeError" in rendered


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (OSError(5, "Input/output error"), "[Errno 5]"),
        (
            PermissionError(13, "Permission denied", "/site-packages/METADATA"),
            "/site-packages/METADATA",
        ),
        (
            UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte"),
            "position 0",
        ),
    ],
    ids=["errno", "filename", "decode-position"],
)
def test_the_cause_keeps_what_separates_one_failure_from_another(
    metadata_version, exc, expected
) -> None:
    """A bare type name is not enough to act on, which is the whole point of
    reporting the catch rather than swallowing it.

    `OSError` alone cannot distinguish EIO (hardware) from ESTALE (remount) from
    EACCES (`chmod`) — three different remedies, i.e. exactly the distinction
    this module exists to preserve — and the remedy text defers to this line
    precisely because the catch is broader than it can speak to. `format_
    exception_only` also retains **no frames**, which matters because `detail`
    becomes a module global at import.
    """

    def _raise(_name: str) -> str:
        raise exc

    metadata_version(_raise)
    detail = resolve_version().detail
    assert detail is not None and expected in detail


def test_a_detail_is_only_rendered_when_there_is_one() -> None:
    """The two older causes carry no exception, and must not grow an empty
    `cause:` line reading as if something were withheld.

    Asserted against the module's own `_CAUSE_PREFIX` rather than a literal
    `"cause:"`: a rename would otherwise leave this asserting the absence of a
    string that never appears anywhere, i.e. an assertion that cannot fail. Same
    trap as `_printed_version`'s `rpartition` (session 24).
    """
    rendered = unknown_version_diagnostic(VersionSource.NOT_INSTALLED, detail=None)
    assert rendered is not None
    assert _CAUSE_PREFIX not in rendered
    # And the positive control, so the check above is known to be capable of
    # firing on the same helper.
    with_detail = unknown_version_diagnostic(
        VersionSource.METADATA_UNREADABLE, detail="OSError"
    )
    assert with_detail is not None and _CAUSE_PREFIX in with_detail


def test_a_detail_is_appended_to_the_remedy_never_substituted_for_it() -> None:
    """The remedy is what an operator acts on; the cause sits below it.

    Asserted as a *relation* between the two renderings rather than as
    containment, because `"OSError" in rendered` and `_CAUSE_PREFIX in rendered`
    are both satisfied by a rendering that returns the cause line **alone** —
    and since `resolve_version` always sets `detail` on this path, that is the
    only string a #296-affected operator ever sees. A rendering that dropped the
    remedy would leave them `  cause: OSError` and no remedy at all, with every
    other assertion in this module still green.
    """
    plain = unknown_version_diagnostic(VersionSource.METADATA_UNREADABLE, detail=None)
    with_detail = unknown_version_diagnostic(
        VersionSource.METADATA_UNREADABLE, detail="OSError"
    )
    assert plain is not None and with_detail is not None
    assert with_detail == f"{plain}\n{_CAUSE_PREFIX}OSError"
    # Its own line — pins the newline the `_CAUSE_PREFIX` comment promises.
    assert with_detail.splitlines()[-1] == f"{_CAUSE_PREFIX}OSError"


def test_a_healthy_source_carrying_a_swallowed_exception_is_rejected() -> None:
    """The one pairing the renderer cannot express, made loud rather than
    silent: it used to return `None`, discarding the exception *and* reporting
    the install healthy — #291's shape, in the function written to end it."""
    with pytest.raises(ValueError, match="cannot both be true"):
        unknown_version_diagnostic(VersionSource.INSTALLED, detail="OSError")


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(1)])
def test_the_broad_catch_does_not_swallow_a_base_exception(metadata_version, exc) -> None:
    """`except Exception` is deliberate and must not be widened to
    `BaseException`. An operator's Ctrl-C during a slow metadata read on a
    hung network mount has to interrupt the process, not be reported as a
    damaged install and then hidden behind a version string.
    """

    def _raise(_name: str) -> str:
        raise exc

    metadata_version(_raise)
    with pytest.raises(type(exc)):
        resolve_version()


def test_a_healthy_resolution_carries_no_detail(metadata_version) -> None:
    """`detail` is failure bookkeeping; a real version has nothing to explain."""
    metadata_version(lambda _name: "1.2.3+sentinel")
    assert resolve_version().detail is None


def test_an_unreadable_resolution_cannot_be_built_without_its_cause() -> None:
    """The invariant stated as an *obligation*, not merely a permission.

    `unresolvable(METADATA_UNREADABLE)` used to be reachable and produced a
    remedy with no `cause:` line — the broad `except Exception` reporting
    nothing about what it caught. Enforced at construction, in the same
    layering `VersionSource` uses one level up, so the slip cannot reach CI.
    """
    with pytest.raises(ValueError, match="must carry a detail"):
        ResolvedVersion(UNKNOWN_VERSION, VersionSource.METADATA_UNREADABLE)


@pytest.mark.parametrize(
    "source",
    [VersionSource.INSTALLED, VersionSource.NOT_INSTALLED,
     VersionSource.METADATA_INCOMPLETE],
)
def test_a_cause_that_raised_nothing_cannot_carry_a_detail(source) -> None:
    """The other direction. These causes are reached without anything being
    raised, so a detail on one could only be a caller pairing them by mistake —
    and it would render an empty-looking `cause:` line as if a real one were
    being withheld."""
    with pytest.raises(ValueError, match="must carry no detail"):
        ResolvedVersion(UNKNOWN_VERSION, source, "OSError")


def test_a_known_version_carries_no_diagnostic() -> None:
    """The overwhelmingly common case. A warning here would train operators to
    ignore the line that matters."""
    assert unknown_version_diagnostic(VersionSource.INSTALLED, detail=None) is None


def test_each_unknown_cause_names_a_distinct_remedy() -> None:
    """The point of splitting the causes: `uv sync` does not repair a dist-info
    that is already present, `--reinstall` is wasted on a tree that has none,
    and neither repairs a filesystem that cannot serve the file. If the branches
    said the same thing, the split would be decoration.
    """
    never = unknown_version_diagnostic(VersionSource.NOT_INSTALLED, detail=None)
    damaged = unknown_version_diagnostic(VersionSource.METADATA_INCOMPLETE, detail=None)
    unreadable = unknown_version_diagnostic(
        VersionSource.METADATA_UNREADABLE, detail=None
    )
    assert never is not None and damaged is not None and unreadable is not None
    assert len({never, damaged, unreadable}) == 3
    assert "uv tool install localmail" in never
    assert "--reinstall" in damaged
    assert "--reinstall" not in never
    # The one remedy no reinstall can supply, and the reason this is a third
    # cause rather than a second spelling of the damaged-install one.
    assert "filesystem" in unreadable


def test_every_unknown_source_has_a_diagnostic() -> None:
    """Exhaustiveness — the backstop to the enum's own construction guard.

    A cause with no message returns `None`, and `None` is also how this module
    says "healthy, stay quiet": that is #291 itself one level up, with the flag
    looking fine. Declaring the remedy on the member means *omitting* it now
    raises `TypeError` at class creation, so the common slip cannot reach CI.
    What construction cannot catch is a member written `("x", None)` on
    purpose, which is what this test is for.
    """
    unmapped = [
        source
        for source in VersionSource
        if source is not VersionSource.INSTALLED
        and unknown_version_diagnostic(source, detail=None) is None
    ]
    assert unmapped == []


def test_a_cause_declared_without_a_remedy_fails_at_import() -> None:
    """The construction guard the test above backstops.

    `_DIAGNOSTICS`-as-a-dict made forgetting a remedy a silent `None`; the
    member payload makes it a `TypeError` before the module finishes importing.
    Reproduced on a stand-in rather than by mutating `VersionSource`, which
    cannot be extended after creation.
    """
    with pytest.raises(TypeError, match="diagnostic"):

        class _Incomplete(Enum):
            diagnostic: str | None

            def __new__(cls, value: str, diagnostic: str | None) -> "_Incomplete":
                member = object.__new__(cls)
                member._value_ = value
                member.diagnostic = diagnostic
                return member

            FORGOTTEN = "no-remedy-supplied"


def test_the_sentinel_is_named_not_repeated() -> None:
    """Both unknown branches report the *same* string, and it is the one every
    other reader compares against. #291's first scope item: the literal was
    written out twice in `__init__.py` and quoted a third time in a comment."""
    assert UNKNOWN_VERSION == "0.0.0+unknown"
