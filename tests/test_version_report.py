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
    reject_empty_diagnostic,
    render_exception_chain,
    resolve_version,
    unknown_version_diagnostic,
)
from localmail.version_report import (
    _CAUSE_PREFIX,
    _CHAIN_SEPARATOR,
    _MAX_CHAIN_LINKS,
    _MAX_DETAIL_CHARS,
    _TRUNCATION_MARKER,
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
    # The type name leads for both of these, so `str(exc)` alone (empty for much
    # of what fails here) cannot satisfy this. Not an absolute across every
    # exception — see `test_a_rendering_that_raises_degrades_to_the_type_name`.
    assert resolved.detail.startswith(type(exc).__name__)


class _ThirdPartyHookError(Exception):
    """Stands in for whatever a third-party `sys.meta_path` finder raises.

    Deliberately outside every builtin hierarchy the module names, because that
    is the point: the catch is `except Exception` precisely so it does not need
    to know this class exists.
    """


@pytest.mark.parametrize(
    "exc",
    [MemoryError(), RecursionError(), _ThirdPartyHookError("finder exploded")],
    ids=["memory", "recursion", "third-party-hook"],
)
def test_the_catch_is_broad_enough_for_causes_that_are_not_about_the_file(
    metadata_version, exc
) -> None:
    """#296's actual claim: enforced against *every* exception, not a list.

    The two reproductions above are both file-shaped, so a revert of the broad
    catch to `except (UnicodeDecodeError, OSError)` — the pre-#296 shape —
    satisfied every other test in this suite while `import localmail` died again
    on anything else. These three are the causes the module docstring and the
    remedy wording explicitly name as reachable and *not* about the file, which
    is why they are the ones worth pinning: if the catch is ever narrowed to a
    type list, it will be narrowed to a list of file errors.
    """

    def _raise(_name: str) -> str:
        raise exc

    metadata_version(_raise)
    resolved = resolve_version()
    assert resolved.source is VersionSource.METADATA_UNREADABLE
    assert resolved.detail is not None and type(exc).__name__ in resolved.detail


def test_a_rendering_that_raises_degrades_to_the_type_name(metadata_version) -> None:
    """The reporting step may not raise either — it runs inside the handler.

    `traceback.format_exception_only` is not total: it calls `.rstrip()` on
    `SyntaxError.text` unconditionally, so an exception carrying a non-`str`
    there makes the *renderer* raise, straight back out of `except Exception`
    and out of `import localmail`. That is #296's defect restored by #296's own
    fix; unguarded it killed the interpreter outright ("lost sys.stderr"). The
    fallback is the bounded pre-#296 rendering, so the cause degrades rather
    than being lost.
    """
    hostile = SyntaxError("bad")
    hostile.text = object()  # type: ignore[assignment]
    hostile.lineno = 1
    hostile.filename = "<meta-path-finder>"

    def _raise(_name: str) -> str:
        raise hostile

    metadata_version(_raise)
    resolved = resolve_version()
    assert resolved.source is VersionSource.METADATA_UNREADABLE
    assert resolved.detail == "SyntaxError"


def test_a_vast_exception_message_cannot_become_an_unbounded_global(
    metadata_version,
) -> None:
    """`detail` lives for the process and is logged in full at every startup.

    `format_exception_only` embeds the whole of `str(exc)` plus every PEP 678
    note, and a third-party hook chooses both. The pre-#296 type name was
    bounded by construction; this keeps a bound while keeping the errno,
    filename and decode offset that motivated the richer rendering.
    """

    def _raise(_name: str) -> str:
        raise RuntimeError("x" * 100_000)

    metadata_version(_raise)
    detail = resolve_version().detail
    assert detail is not None
    assert len(detail) < 1_000
    assert detail.startswith("RuntimeError")


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


def test_the_cause_follows_a_wrapper_to_the_exception_that_names_the_fault(
    metadata_version,
) -> None:
    """#303: `format_exception_only` renders only the *outermost* exception.

    The rendering was chosen over a bare type name because it keeps the errno,
    the filename and the decode offset — the three things that separate EIO from
    ESTALE from EACCES. When the interesting exception is a `__cause__` it
    discarded exactly what it was chosen to preserve, and sent the operator to a
    remedy that says "read the cause below first" over a cause naming nothing
    actionable.

    A wrapper is not hypothetical here: the module docstring names a third-party
    `sys.meta_path` finder as a reachable trigger, and wrapping a low-level
    `OSError` in a library-specific error is the normal thing such a finder does.
    """

    def _raise(_name: str) -> str:
        raise RuntimeError("finder failed") from OSError(
            5, "Input/output error", "/nfs/site-packages/localmail.dist-info/METADATA"
        )

    metadata_version(_raise)
    detail = resolve_version().detail
    assert detail is not None
    # The exception that was actually raised still leads — it is what the
    # traceback would have shown, and the reader needs to recognise it.
    assert detail.startswith("RuntimeError")
    # ...and the two things the remedy tells them to act on survive it.
    assert "[Errno 5]" in detail
    assert "/nfs/site-packages/localmail.dist-info/METADATA" in detail


def _chain_of(depth: int) -> BaseException:
    """`depth` exceptions, each raised *from* the next, innermost named last."""
    exc: BaseException = OSError(5, "innermost")
    for level in reversed(range(depth - 1)):
        try:
            raise RuntimeError(f"wrapper {level}") from exc
        except RuntimeError as raised:
            exc = raised
    return exc


def test_an_unwrapped_exception_gains_no_chain_decoration() -> None:
    """The overwhelmingly common shape, and the regression guard for #303's fix.

    Both causes #296 actually reproduced — a bare `OSError`, a
    `UnicodeDecodeError` — are unwrapped, so a chain walk that appended a
    separator, a trailing marker or an empty link would change every real
    rendering to buy a case that had not happened yet.
    """
    rendered = render_exception_chain(OSError(5, "Input/output error"))
    assert _CHAIN_SEPARATOR not in rendered
    assert rendered == "OSError: [Errno 5] Input/output error"


def test_a_detached_context_is_not_reported_as_a_cause() -> None:
    """`raise X from None` detaches the context on purpose; printing it anyway
    contradicts the author and `traceback`'s own behaviour.

    The distinction matters on this path: a finder that catches an `OSError` and
    deliberately re-raises without it is saying the `OSError` is not the fault to
    act on, and the remedy line sends the operator to whatever this names.
    """
    try:
        try:
            raise OSError(5, "detached")
        except OSError:
            raise RuntimeError("replaced") from None
    except RuntimeError as exc:
        rendered = render_exception_chain(exc)
    assert rendered == "RuntimeError: replaced"
    assert "detached" not in rendered


def test_a_cause_that_is_falsy_is_still_followed() -> None:
    """The walk asks whether there *is* a cause, not whether it is truthy.

    An exception is an ordinary object: one whose class defines `__bool__` or
    `__len__` — an error type that doubles as a collection of what failed is the
    realistic shape — is falsy while being perfectly present. Written as
    `__cause__ or __context__` such a link is skipped, which is #303's own defect
    (the rendering dropping the exception that names the fault) reintroduced by
    the walk added to fix it.

    Note the fallback cannot cover for it: assigning `__cause__` sets
    `__suppress_context__`, so the skipped link is not replaced by the context —
    it is simply lost.
    """

    class FalsyError(Exception):
        def __bool__(self) -> bool:
            return False

    outer = RuntimeError("finder failed")
    outer.__cause__ = FalsyError("the real fault")

    rendered = render_exception_chain(outer)
    assert rendered.startswith(f"RuntimeError: finder failed{_CHAIN_SEPARATOR}")
    assert rendered.endswith("FalsyError: the real fault")


def test_an_implicit_context_is_followed() -> None:
    """A bare `raise` inside an `except` sets `__context__`, not `__cause__`.

    Third-party code raises this shape far more often than it writes `from`, so
    following only `__cause__` would miss most real wrappers — the case #303 is
    about.
    """
    try:
        try:
            raise OSError(5, "Input/output error", "/mnt/METADATA")
        except OSError:
            raise RuntimeError("finder failed")
    except RuntimeError as exc:
        rendered = render_exception_chain(exc)
    assert rendered.startswith("RuntimeError: finder failed")
    assert "[Errno 5]" in rendered and "/mnt/METADATA" in rendered


def test_a_cyclic_chain_terminates() -> None:
    """Reachable, not theoretical: an exception re-raised while its own cause is
    being handled closes the loop. Unguarded this is an infinite walk on the
    import path — a worse failure than the one the chain exists to fix, since it
    hangs rather than raising.
    """
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first
    rendered = render_exception_chain(first)
    assert rendered == (
        f"RuntimeError: first{_CHAIN_SEPARATOR}RuntimeError: second"
        f"{_CHAIN_SEPARATOR}{_TRUNCATION_MARKER}"
    )


def test_a_long_chain_is_truncated_to_the_link_bound() -> None:
    """The other bound. `detail` becomes a module global and is logged in full at
    every startup, and a chain's length is chosen by whatever raised."""
    rendered = render_exception_chain(_chain_of(_MAX_CHAIN_LINKS + 4))
    # `max_links` renderings, plus the marker that says so.
    assert rendered.count(_CHAIN_SEPARATOR) == _MAX_CHAIN_LINKS
    # The innermost link is what gets dropped, which is the right end to lose:
    # the exception actually raised is the one the reader must recognise.
    assert "innermost" not in rendered


def test_a_truncated_chain_says_that_it_was_truncated() -> None:
    """Both early exits mark the rendering; a natural end does not.

    The end a cut walk drops is the innermost, which is where the errno and the
    filename are — so an unmarked truncation hands the operator a degraded cause
    in a shape indistinguishable from a complete one, under a remedy line that
    tells them to read it first. That is #291's defect (an unresolvable answer
    presented as an answer) one layer down, which is why the marker is a pin and
    not a nicety.
    """
    cyclic = RuntimeError("first")
    cyclic.__cause__ = RuntimeError("second")
    cyclic.__cause__.__cause__ = cyclic

    for cut in (_chain_of(_MAX_CHAIN_LINKS + 1), cyclic):
        assert render_exception_chain(cut).endswith(
            f"{_CHAIN_SEPARATOR}{_TRUNCATION_MARKER}"
        )

    # The common shapes #296 actually reproduced end naturally and must stay
    # byte-identical — a marker on every rendering would say nothing.
    assert _TRUNCATION_MARKER not in render_exception_chain(OSError(5, "eio"))
    assert _TRUNCATION_MARKER not in render_exception_chain(
        _chain_of(_MAX_CHAIN_LINKS)
    )


def test_a_chain_of_vast_messages_stays_bounded(metadata_version) -> None:
    """The character ceiling applies to the joined chain, not per link.

    `test_a_vast_exception_message_cannot_become_an_unbounded_global` proves the
    single-exception case; a chain multiplies it by `_MAX_CHAIN_LINKS`, so the
    bound has to be applied after the join or it is five times looser than it
    reads.

    Asserted against `_MAX_DETAIL_CHARS` rather than a round number: at the
    shipped values a *per-link* ceiling would render 1014 characters, so a
    literal `1_000` catches the regression by a 14-character margin that
    shortening `_CHAIN_SEPARATOR` — or lowering the ceiling — would silently
    spend, leaving a pin that reads strict and proves nothing.
    """

    def _raise(_name: str) -> str:
        raise RuntimeError("x" * 100_000) from OSError(5, "y" * 100_000)

    metadata_version(_raise)
    detail = resolve_version().detail
    assert detail is not None
    assert len(detail) <= _MAX_DETAIL_CHARS + len(_TRUNCATION_MARKER)


def test_a_whitespace_only_rendering_does_not_kill_the_import(monkeypatch) -> None:
    """`__post_init__` rejects a blank detail by *raising*, on the import path.

    So the fallback guarding it has to catch whitespace, not merely emptiness: a
    bare `or` passes `"   "` through as truthy and straight into that raise —
    `import localmail` dying inside the handler written to stop `import
    localmail` dying, which is #296 turned on itself.

    Unreachable through the real renderer today, and deliberately pinned anyway:
    what makes it unreachable is that `_CHAIN_SEPARATOR` happens to contain
    letters, so the guard's correctness rests on a constant it does not own and
    nothing else would notice that coming apart.
    """
    monkeypatch.setattr(
        "localmail.version_report.render_exception_chain", lambda _exc: "   \n\t "
    )
    resolved = ResolvedVersion.unreadable(OSError(5, "Input/output error"))
    assert resolved.detail == "OSError"


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


@pytest.mark.parametrize("blank", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_a_blank_detail_is_rejected_rather_than_rendered(blank) -> None:
    """The field's stated invariant is non-emptiness, not non-`None`-ness.

    `detail=""` satisfied `is not None`, so it constructed cleanly and rendered
    a dangling `cause:` with nothing after it — verbatim the "reads as if a
    detail were being withheld" outcome the field comment says must never
    happen. `log_version_diagnostic`'s falsy guard does not catch it either,
    because the *whole* diagnostic is non-empty.
    """
    with pytest.raises(ValueError, match="non-blank"):
        ResolvedVersion(UNKNOWN_VERSION, VersionSource.METADATA_UNREADABLE, blank)


@pytest.mark.parametrize(
    "source",
    [
        VersionSource.NOT_INSTALLED,
        VersionSource.METADATA_INCOMPLETE,
        VersionSource.METADATA_UNREADABLE,
    ],
)
def test_a_failed_resolution_cannot_carry_a_real_version(source) -> None:
    """The older of the two pairings, and the one #291 is actually about.

    A failed source paired with a real-looking version yields `__version__`
    reporting something plausible while `__version_diagnostic__` explains a
    failure — or, inverted, the sentinel with no diagnostic at all, which is
    #291's shape exactly. Nothing enforced this until now; `unresolvable`
    supplying the sentinel was discipline, not a guarantee.
    """
    detail = "OSError" if source is VersionSource.METADATA_UNREADABLE else None
    with pytest.raises(ValueError, match="must carry"):
        ResolvedVersion("0.3.0", source, detail)


def test_the_sentinel_is_still_allowed_to_be_a_real_version() -> None:
    """The converse is deliberately *not* asserted.

    A pyproject that ever declared `0.0.0+unknown` would otherwise fail `import
    localmail` over a cosmetic collision — and the module's first rule is that
    import does not fail. The version guard is one-directional on purpose.
    """
    assert ResolvedVersion.installed(UNKNOWN_VERSION).version == UNKNOWN_VERSION


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


def test_the_unreadable_remedy_defers_to_the_cause_instead_of_asserting_one() -> None:
    """CLAUDE.md: "Do not restore an unconditional filesystem claim here."

    The earlier wording asserted a corrupt file or a faulty filesystem for
    *every* exception the broad catch sees, which sends an OOMing host to `fsck`
    a healthy volume. Both wordings contain the word "filesystem", so the
    containment check above is satisfied by exactly the string this test exists
    to keep out — pinning the rule needs an assertion the old wording cannot
    pass.
    """
    unreadable = unknown_version_diagnostic(
        VersionSource.METADATA_UNREADABLE, detail=None
    )
    assert unreadable is not None
    # Defers: the operator is sent to the cause line first, and the filesystem
    # claim is scoped to the one exception type it holds for.
    assert "read the cause below first" in unreadable
    assert "For an OSError" in unreadable
    # The old wording's unconditional claim, in the two spellings it had.
    assert "the file is corrupt" not in unreadable
    assert "filesystem holding it is faulty" not in unreadable


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
        # Falsy, not `is None`: an empty remedy is swallowed by
        # `log_version_diagnostic`'s falsy guard just as a missing one is, so
        # asserting only against `None` left the same hole one value over.
        and not unknown_version_diagnostic(source, detail=None)
    ]
    assert unmapped == []


def test_a_cause_declared_with_an_empty_remedy_fails_at_import() -> None:
    """The gap between "supplied a remedy" and "supplied a *real* one".

    A member written `("new-cause", "")` supplies both payload elements, so the
    signature is satisfied and no `TypeError` fires; the test above passed it
    while `is None` was the predicate; and `log_version_diagnostic`'s falsy
    guard then discards it. Net effect: a broken install reported as healthy on
    `serve` and `run` — #291 one level up, which is the exact outcome declaring
    the remedy on the member is supposed to make impossible.
    """
    with pytest.raises(TypeError, match="empty diagnostic"):
        reject_empty_diagnostic("new-cause", "")
    with pytest.raises(TypeError, match="empty diagnostic"):
        reject_empty_diagnostic("new-cause", "   ")
    # The two legitimate shapes are untouched: the healthy member's silence, and
    # a real remedy.
    assert reject_empty_diagnostic("installed", None) is None
    assert reject_empty_diagnostic("new-cause", "do this") == "do this"


def test_every_declared_source_went_through_the_diagnostic_guard() -> None:
    """The rule above is only worth anything if `VersionSource` applies it.

    Enum machinery replaces `__new__` after class creation, so a test cannot
    build a stand-in that calls the production one — which is why the rule is a
    module-level function. This is the other half: every shipped member's
    payload satisfies it, so the guard is demonstrably wired to the type rather
    than merely existing beside it.
    """
    for source in VersionSource:
        assert (
            reject_empty_diagnostic(source.value, source.diagnostic)
            is source.diagnostic
        )


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
