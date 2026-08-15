# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Resolving localmail's own version, and what to tell an operator when it fails.

`pyproject.toml` carries the only version literal in the Python tree;
`localmail.__version__` reads it back from the installed distribution metadata
so the two cannot disagree. When that read fails the attribute degrades to
`UNKNOWN_VERSION` rather than raising — import must not fail, and `/v1/version`
emitting `server_version: null` breaks the GUI's connect probe, which decodes
that field as a non-optional String.

The degradation was silent (#291): `localmail --version` printed
`0.0.0+unknown` with exit 0 and nothing on stderr, i.e. reported "the version
could not be determined" in a format indistinguishable from a successful
answer, at the one moment an operator is diagnosing a broken install. This
module holds the resolution and the operator-facing wording; `cli.py` decides
*where* to put it (stdout stays the machine-readable version line, the
diagnostic goes to stderr).

**The failure causes are kept apart because their remedies differ**, which is
the only reason to read the line at all: nothing is installed, so install
something; a dist-info is present but carries no `Version:` header, so replace
what is there; or the metadata could not be read at all, which no reinstall
fixes if the filesystem under it is the problem. `uv sync` does not repair the
second. They used to collapse to one string.

**`import must not fail` is enforced against every exception, not one (#296).**
`importlib.metadata.version` reads `METADATA` as UTF-8 through a `suppress(...)`
list that covers neither `UnicodeDecodeError` nor a generic `OSError` (checked
against CPython 3.12/3.13), so a file in another encoding — or an EIO on a
network-mounted `site-packages` — used to propagate out of `import localmail`
and kill every entry point with a bare traceback, **including `--version`**,
whose whole purpose is diagnosing a broken install. The broad catch that closes
it is only defensible because it *reports* what it caught:
`ResolvedVersion.unreadable` renders the exception onto `.detail` and the line
carries it below the remedy.

**The reporting step is guarded too**, which is not a belt-and-braces flourish:
`traceback.format_exception_only` is not total and it allocates, so unguarded it
raised straight back out of the handler and killed `import localmail` — #296
restored by #296's own fix, on the very `MemoryError` the remedy text names.
The rule is the same one the read obeys: nothing on this path may raise, so
every step on it degrades instead. See `ResolvedVersion.unreadable`.

**Scope, so the claim above is not read wider than it is (#305).** What survives
here is an unreadable *METADATA*, and only that. `localmail --version` still
dies on the *other* broken install: `cli.py` imports the daemon — and so
`sqlparse`, `psycopg`, `keyring` — at module scope, so a partial `uv sync` that
dropped any third-party dependency kills the command with a bare traceback
before click parses the flag. Reproduced by blocking one module on
`sys.meta_path`: `import localmail` succeeds and resolves its version,
`import localmail.cli` does not. Making that survivable means deferring those
imports into the command bodies that need them, which is a change to `cli.py`
rather than to this module, and belongs with the refactor it already owes.

**The catch is broader than the remedy can speak to, and the wording admits
it.** `MemoryError`, `RecursionError` and anything a third-party `sys.meta_path`
finder raises are all `Exception` subclasses reached through this branch, and
none of them is a corrupt METADATA. An earlier wording asserted a faulty
filesystem for every one of them, which would send an OOMing host to `fsck` a
healthy volume — the module's own "the causes are kept apart because the
remedies differ" principle, inverted at the point it adds a cause. The remedy
now defers to the cause line, which is why that line carries
`format_exception_only` output (errno, filename, decode offset) rather than a
bare type name.

Pure except `resolve_version` (whose one impure step is the metadata read) and
`log_version_diagnostic` (which exists so the processes that have no stderr
convention of their own cannot each invent one).
"""
from __future__ import annotations

import importlib.metadata
import logging
import traceback
from dataclasses import dataclass
from enum import Enum

#: The distribution to look up — also the name quoted back at the operator in
#: every remedy, so a rename cannot leave the advice pointing at the old one.
DISTRIBUTION_NAME = "localmail"

#: What `localmail.__version__` reports when the metadata cannot be read.
#: Named rather than repeated: it was written out twice in `__init__.py` and
#: quoted a third time in a comment, and no reader compared against any of them.
UNKNOWN_VERSION = "0.0.0+unknown"

#: The level every log consumer sees this at. Named because the severity word
#: below is derived from it: the two were written independently and disagreed
#: (#302), so journald showed `ERROR ... warning: ...`. The rationale for the
#: level itself is on `log_version_diagnostic`.
_REPORT_LEVEL = logging.ERROR

#: The severity word every remedy opens with, derived from the level rather than
#: written beside it (#302).
#:
#: One string serves two consumers and has to be right for both. `--version`
#: writes it to stderr through click, where there is no level and this word is
#: the *only* severity marker; `log_version_diagnostic`'s callers hand it to
#: `logging`, where the level is the marker — and on the paths that reach
#: `logging.lastResort` (no formatter, so no level is printed) the word is again
#: the only one. Deriving it means the two cannot be changed apart, which is the
#: same one-authority call `pgtext.strip_nuls` and `text_empty.is_blank` make.
_SEVERITY_PREFIX = f"{logging.getLevelName(_REPORT_LEVEL).lower()}: "

_NEVER_INSTALLED_REMEDY = (
    f"{_SEVERITY_PREFIX}the {DISTRIBUTION_NAME} version could not be determined — no "
    f"distribution metadata is installed for it here, so this is a source "
    f"tree that was never installed.\n"
    f"  remedy: run `uv sync` in a development checkout, or "
    f"`uv tool install {DISTRIBUTION_NAME}`."
)

_DAMAGED_INSTALL_REMEDY = (
    f"{_SEVERITY_PREFIX}the {DISTRIBUTION_NAME} version could not be determined — its "
    f"distribution metadata is installed but carries no version, so the "
    f"install is damaged.\n"
    f"  remedy: run `uv sync --reinstall-package {DISTRIBUTION_NAME}` in a "
    f"development checkout, or `uv tool install --reinstall "
    f"{DISTRIBUTION_NAME}`."
)

_UNREADABLE_METADATA_REMEDY = (
    f"{_SEVERITY_PREFIX}the {DISTRIBUTION_NAME} version could not be determined — "
    f"reading its distribution metadata raised.\n"
    f"  remedy: read the cause below first. The catch behind this line is broad "
    f"on purpose (import must not fail), so it also sees failures that are not "
    f"about the file at all — a MemoryError, or a third-party import hook. For "
    f"an OSError, check the filesystem under site-packages before anything else: "
    f"a reinstall cannot fix a failing mount. Otherwise run `uv sync "
    f"--reinstall-package {DISTRIBUTION_NAME}` in a development checkout, or "
    f"`uv tool install --reinstall {DISTRIBUTION_NAME}`."
)

#: Prefix for the swallowed exception's rendering. Its own line so the remedy
#: stays the thing an operator acts on and the technical cause sits below it.
#: A multi-line rendering (PEP 678 notes, a `SyntaxError`) prefixes only its
#: first line — the continuation lines are the exception's own shape and
#: re-indenting them would corrupt a caret line.
_CAUSE_PREFIX = "  cause: "

#: Ceiling on a rendered cause. `str(exc)` is attacker-shaped only in the sense
#: that a third-party import hook picks it, but the value becomes a module
#: global for the life of the process and is logged in full at every startup,
#: so it is bounded rather than trusted. Generous enough that no realistic
#: `OSError`/`UnicodeDecodeError` rendering is touched.
_MAX_DETAIL_CHARS = 500

#: How many links of a `__cause__`/`__context__` chain the cause line renders.
#: Bounded for the same reason the character count is, and for one more: this
#: walk runs inside the handler that may not fail, on the import path. Five is
#: past anything a real wrapper stack produces — a finder wrapping an `OSError`
#: is two — while still terminating a pathological one.
_MAX_CHAIN_LINKS = 5

#: Between two links, read outermost-first: the exception that was raised, then
#: what it was raised from. Prose rather than a bare arrow because the line is
#: read by an operator, not parsed.
_CHAIN_SEPARATOR = " <- caused by "

#: Appended wherever a rendering was cut short. Named once because both bounds
#: on this path use it — the character ceiling in `ResolvedVersion.unreadable`
#: and the link/cycle bound in `render_exception_chain` — and a marker that
#: appears for one truncation but not the other is worse than none: it teaches
#: the reader that an unmarked line is complete.
_TRUNCATION_MARKER = "…"


def render_one_exception(exc: BaseException) -> str:
    """Render `exc` alone, degrading to its type name rather than raising.

    `traceback.format_exception_only` is not total — it calls `.rstrip()` on
    `SyntaxError.text` unconditionally, so an exception carrying a non-`str`
    there makes the *renderer* raise — and it allocates, which is what fails
    again under the `MemoryError` the remedy text names. Per link rather than
    once around the whole walk so one hostile link costs its own detail instead
    of the entire chain's; `render_exception_chain`'s caller still guards the
    walk itself, since reading `__cause__` off a hostile object can raise too.
    """
    try:
        return "".join(traceback.format_exception_only(type(exc), exc)).strip()
    except Exception:
        return type(exc).__name__


def render_exception_chain(
    exc: BaseException, *, max_links: int = _MAX_CHAIN_LINKS
) -> str:
    """Render `exc` and what it was raised from, outermost first (#303).

    Pure. The exception that was raised leads — it is what a traceback would
    have shown, and what the reader has to recognise — and each link it was
    raised *from* follows, because that is where an errno or a filename usually
    is. `format_exception_only` renders only the outermost, so on a wrapped
    exception it discarded precisely the detail it was chosen over a bare type
    name to keep.

    **Follows `__cause__` first, then `__context__` unless suppressed**, which is
    what `raise X from None` asks for and what `traceback` itself honours.
    Ignoring `__suppress_context__` would print a chain the author explicitly
    detached.

    Two bounds and a cycle guard, all three because this runs on the import path
    inside a handler that may not fail: `max_links` ends a pathological wrapper
    stack, the identity set ends a `__context__` cycle (reachable — an exception
    re-raised inside the handling of its own cause), and the character ceiling is
    applied by the caller to the joined result. Retains **no frame references**,
    which is what makes it safe for a value that becomes a module global: every
    link goes through `format_exception_only`, never `format_exception`.

    **A walk cut short by either of the first two ends in `_TRUNCATION_MARKER`**,
    so a partial chain cannot be read as a complete one — the end it loses is the
    innermost, i.e. the errno this function exists to surface. A chain that ends
    naturally gains nothing, which is what keeps the overwhelmingly common
    unwrapped rendering byte-identical.

    The join is truncated as a whole rather than per link, so a pathological
    outermost message can still crowd out an inner errno. Accepted: one bound is
    one rule, and a per-link budget would truncate the common *unwrapped*
    rendering — the case that motivated the ceiling's generous size — to a fifth
    of it.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(parts) < max_links and id(current) not in seen:
        seen.add(id(current))
        parts.append(render_one_exception(current))
        # `is not None`, never `or`: an exception whose class defines
        # `__bool__`/`__len__` is falsy while being perfectly present, and `or`
        # would skip it — this rendering dropping the exception that names the
        # fault, which is the whole of #303. Nor does the fallback cover for it:
        # assigning `__cause__` sets `__suppress_context__`, so a skipped cause
        # is lost rather than replaced by the context. Read lazily so a hostile
        # `__context__` is not touched when a cause already answered.
        cause = current.__cause__
        if cause is not None:
            current = cause
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    if current is not None:
        # The walk stopped early — `max_links`, or the cycle guard — and the end
        # it drops is the innermost, which is where the errno and the filename
        # are. Marking it is not decoration: an unmarked truncation is a
        # degraded diagnostic presented as a complete one, which is the shape of
        # #291 and #302 both, in the module written to end it.
        parts.append(_TRUNCATION_MARKER)
    return _CHAIN_SEPARATOR.join(parts)


def reject_empty_diagnostic(value: str, diagnostic: str | None) -> str | None:
    """Return `diagnostic`, or raise if it is present but says nothing.

    `None` is the one healthy member's "stay quiet"; `""` is a member that
    *meant* to carry a remedy and shipped an empty one, which
    `log_version_diagnostic`'s falsy guard then swallows — a broken install
    reported as fine, i.e. the #291 shape that declaring the remedy on the
    member exists to prevent. Supplying both payload elements satisfies
    `__new__`'s signature, so nothing else distinguishes the two.

    A module-level function rather than an inline check because enum machinery
    replaces `__new__` after class creation, so a test cannot reach the
    production one to prove the rule fires for a *future* member. This is the
    rule; `VersionSource.__new__` is its only caller.
    """
    if diagnostic is not None and not diagnostic.strip():
        raise TypeError(
            f"VersionSource.{value} carries an empty diagnostic; use None for "
            f"the healthy member and a real remedy for every other."
        )
    return diagnostic


def reject_empty_wire_name(value: str, wire_name: str) -> str:
    """Reject a blank `wire_name` at class creation.

    `VersionSource.__new__` is the only caller. A member written
    `("x", None, "")` supplies every payload element, so the signature is
    satisfied and no `TypeError` fires — and `/v1/version` would then emit an
    empty string as a real source, which is #291 one level up.

    Module-level rather than an inline check for the reason
    `reject_empty_diagnostic` is: enum machinery replaces `__new__` after class
    creation, so no test can reach the production one to prove the rule fires
    for a future member.

    Raises `ValueError` where `reject_empty_diagnostic` raises `TypeError`,
    deliberately: a blank string is the right *type* with the wrong *value*.
    Both fire during class creation, so either way the failure is a loud import.
    Do not "align" them — the older one is the odd member of the pair.
    """
    if not wire_name.strip():
        raise ValueError(f"VersionSource {value!r} declares an empty wire_name")
    return wire_name


class VersionSource(Enum):
    """Why `__version__` holds the value it does, and what to do about it.

    Carried beside the version rather than inferred from it: a caller cannot
    tell the two failure causes apart from `UNKNOWN_VERSION` alone, and
    string-matching the sentinel to find out is how the remedies drift.

    **The remedy lives on the member, not in a lookup table beside the enum.**
    A `dict[VersionSource, str]` read with `.get()` returns `None` for an
    unmapped member, and `None` is also how this module says "healthy install,
    stay quiet" — so a cause added without a message would report a broken
    install as fine, which is #291 itself one level up. Declared here, a member
    without the pair raises `TypeError` at class creation, i.e. at import,
    rather than in CI. Same by-construction reasoning as `ExtractedText`'s
    `__post_init__` (#249/#266) and `_HttpJsonRewriter`'s `base_url_setting`
    (#235).

    The member *values* are debugging aids, not a wire contract — nothing
    serialises or parses them (contrast `rewrite_note_code`, which is on the
    wire and documented across three surfaces).
    """

    #: The remedy to print, or None for the one member where nothing is wrong.
    #: Annotation only — a bare annotation declares no enum member.
    diagnostic: str | None
    #: The string `/v1/version` emits (#300). Declared, never derived from
    #: `value`: the values below are hyphenated debugging aids, while this
    #: API's wire enums are underscored (`rewrite_note_code` ships
    #: `not_configured`), so derivation would break the convention *and* let a
    #: rename change a parsed contract silently.
    wire_name: str

    def __new__(
        cls, value: str, diagnostic: str | None, wire_name: str
    ) -> VersionSource:
        member = object.__new__(cls)
        member._value_ = value
        member.diagnostic = reject_empty_diagnostic(value, diagnostic)
        member.wire_name = reject_empty_wire_name(value, wire_name)
        return member

    #: The distribution metadata was read; `__version__` is real.
    INSTALLED = ("installed", None, "installed")
    #: No dist-info for this distribution. Note the src layout: a checkout that
    #: was never installed cannot be imported at all (`python -m localmail`
    #: from the repo root is a `ModuleNotFoundError`, so it never reaches this
    #: branch, and the 2B.4 supervisor launches `sys.executable -m localmail`
    #: against an interpreter where the package is installed). The reachable
    #: triggers are an import of the sources without their metadata
    #: (`PYTHONPATH=src`, a vendored copy) and a dist-info removed from under a
    #: live install by a partial sync.
    NOT_INSTALLED = ("not-installed", _NEVER_INSTALLED_REMEDY, "not_installed")
    #: A dist-info exists but its METADATA carries no usable `Version:` header
    #: — a truncated or hand-edited install.
    METADATA_INCOMPLETE = (
        "metadata-incomplete", _DAMAGED_INSTALL_REMEDY, "metadata_incomplete",
    )
    #: The metadata read itself raised (#296) — a METADATA in another encoding
    #: or truncated mid-multibyte, or an `OSError` (EIO, stale NFS handle) from
    #: a network-mounted `site-packages`. Separate from `METADATA_INCOMPLETE`
    #: because the file was never read at all here, and a reinstall is the wrong
    #: first move when the filesystem is what is failing.
    METADATA_UNREADABLE = (
        "metadata-unreadable", _UNREADABLE_METADATA_REMEDY, "metadata_unreadable",
    )


@dataclass(frozen=True)
class ResolvedVersion:
    """The version string, the reason it is what it is, and any caught exception."""

    version: str
    source: VersionSource
    #: The swallowed exception, rendered. Carried by `METADATA_UNREADABLE` and
    #: by nothing else — in *both* directions, enforced below: the other causes
    #: are reached without anything being raised, so an empty `cause:` line
    #: would read as if a detail were being withheld, and an unreadable
    #: resolution *without* one is the broad catch turned silent again.
    detail: str | None = None

    def __post_init__(self) -> None:
        """Reject the pairings the module has no meaning for.

        `VersionSource` earns its remedies at class creation; this is the same
        guard one layer down, for the field that arrived later. Without it
        `unresolvable(METADATA_UNREADABLE)` was reachable and rendered a remedy
        with no cause — the broad `except Exception` reporting nothing about
        what it caught, i.e. #291 inside the module written to end #291.

        A raise, not a normalisation, for `QueueCounts`' reason rather than
        `ExtractedText`'s: a mismatch here is a caller bug, not a value to
        clean up. Neither direction *has* a repair, which is what makes that
        the only option rather than merely the preferred one: an absent
        exception cannot be normalised into existence, and discarding a present
        one is exactly what #296 forbids.

        Three pairings, not one. The `detail` biconditional is the newest; the
        blank-detail and version rules are the two the field's own comment
        already claimed and nothing enforced.

        **The version rule is one-directional on purpose.** A failed resolution
        must carry the sentinel — `unresolvable(INSTALLED)` otherwise yields
        `__version__ = UNKNOWN_VERSION` with `__version_diagnostic__ = None`,
        which is #291's shape exactly. The converse is *not* asserted: a
        pyproject that ever declared `0.0.0+unknown` would then fail `import
        localmail` over a cosmetic collision, and the module's first rule is
        that import does not fail.

        This guard runs on the import path (`__init__.py` resolves at import),
        so a raise here kills every entry point. That is safe only because
        every production constructor satisfies it by construction — `unreadable`
        hardcodes its own source and its own non-empty rendering — leaving this
        to catch a *developer's* mispairing loudly, in CI, where both directions
        are pinned.
        """
        has_detail = self.detail is not None
        should = self.source is VersionSource.METADATA_UNREADABLE
        if has_detail is not should:
            raise ValueError(
                f"{self.source.value} resolutions must carry "
                f"{'a' if should else 'no'} detail; got {self.detail!r}"
            )
        if self.detail is not None and not self.detail.strip():
            raise ValueError(
                f"{self.source.value} resolutions must carry a non-blank "
                f"detail; got {self.detail!r}, which renders a bare "
                f"'{_CAUSE_PREFIX.strip()}' line reading as if one were withheld."
            )
        failed = self.source is not VersionSource.INSTALLED
        if failed and self.version != UNKNOWN_VERSION:
            raise ValueError(
                f"{self.source.value} is a failed resolution and must carry "
                f"{UNKNOWN_VERSION!r}; got {self.version!r}."
            )

    @classmethod
    def installed(cls, version: str) -> ResolvedVersion:
        return cls(version, VersionSource.INSTALLED)

    @classmethod
    def unresolvable(cls, source: VersionSource) -> ResolvedVersion:
        """A cause reached without anything being raised, so there is no detail.

        The sentinel is supplied here, so no caller can pair a real version with
        a failure cause — or spell `UNKNOWN_VERSION` a second time.
        """
        return cls(UNKNOWN_VERSION, source)

    @classmethod
    def unreadable(cls, exc: BaseException) -> ResolvedVersion:
        """The metadata read raised; `exc` is what it raised.

        The rendering rule lives here rather than at the catch site so a second
        one cannot re-decide it — the same one-authority argument `pgtext.
        strip_nuls` and `text_empty.is_blank` already make.

        `format_exception_only` rather than `type(exc).__name__`: it adds errno,
        filename and decode offset — precisely what separates EIO from ESTALE
        from EACCES, three different remedies — and unlike a traceback it
        retains **no frame references**, which matters for a value that becomes
        a module global at import. `str(exc)` alone is empty for much of what
        fails here (`failure_pacing.py`'s reason), so the type name leads in
        every rendering the realistic causes produce. It is not an absolute:
        `format_exception_only` puts the source line first for a `SyntaxError`
        and appends PEP 678 `__notes__` on their own lines, so a `detail` is not
        guaranteed to be one line or to start with the type.

        **Applied to the whole `__cause__` chain, not just `exc` (#303)**, via
        `render_exception_chain`. `format_exception_only` renders one exception,
        so a wrapped one — the normal shape for the third-party finder named
        above — dropped the errno and filename this rendering was chosen to
        keep. See that function for the two bounds and the cycle guard, all of
        which exist because this runs on the import path.

        **The render is itself guarded, and that is the whole reason this method
        exists rather than a one-liner at the catch site.** It runs *inside* the
        `except Exception` whose contract is that nothing escapes into `import
        localmail`, and `format_exception_only` is not total: it calls `.rstrip()`
        on `SyntaxError.text` unconditionally (a third-party `sys.meta_path`
        finder can hand back an object that has no such method), and it allocates
        — a `TracebackException`, a `StackSummary` per chain link, `linecache`
        reads — which is precisely what fails again under the `MemoryError` the
        remedy text names. Unguarded, the reporting step reintroduced the bare
        traceback out of `import localmail` that #296 exists to end. Verified:
        the unguarded form killed the interpreter outright ("lost sys.stderr").
        The fallback is the bounded pre-#296 rendering, so the cause is degraded,
        never lost. `render_one_exception` guards each link as well, so the
        chain survives one hostile member; this outer guard covers the *walk*,
        since reading `__cause__` off a hostile object can raise too.

        Truncated for the same reason it holds no frames: `detail` becomes a
        module global at import and is logged in full at every `serve`/`run`
        start, and `format_exception_only` embeds the whole of `str(exc)` plus
        every note. The old type name was bounded; this keeps a bound while
        keeping the errno/filename/offset that motivated the change.
        """
        try:
            rendered = render_exception_chain(exc)
        except Exception:
            rendered = type(exc).__name__
        if len(rendered) > _MAX_DETAIL_CHARS:
            rendered = rendered[:_MAX_DETAIL_CHARS] + _TRUNCATION_MARKER
        # Tested with `.strip()`, not for truthiness: `__post_init__` rejects a
        # *blank* detail, and it raises — on the import path, inside the handler
        # that may not fail — so a whitespace-only rendering would take `import
        # localmail` down exactly the way #296 exists to prevent. A bare `or`
        # only catches `""`; `"   "` is truthy and would sail through to that
        # raise. Unreachable today, but only because every rendering here either
        # strips to empty or carries the separator's own letters — i.e. the guard
        # was relying on a property of a *different* constant, which is the kind
        # of coupling that comes apart silently.
        return cls(
            UNKNOWN_VERSION,
            VersionSource.METADATA_UNREADABLE,
            rendered if rendered.strip() else type(exc).__name__,
        )


def resolve_version() -> ResolvedVersion:
    """Read the installed distribution metadata, reporting why on failure.

    Reaches through `importlib.metadata` at call time rather than binding
    `version` at import, so a test can stub the lookup and observe the
    *derivation* instead of comparing two values that agree by coincidence.
    """
    try:
        # `version()` returns None — it does not raise — when the dist-info
        # exists but its METADATA has no `Version:` header. typeshed declares
        # it `-> str`, so mypy cannot catch that; the falsy check is what keeps
        # `__version__` a non-empty str for every reader.
        reported = importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        # Must stay ahead of the broad catch below: this is a `ModuleNotFoundError`
        # subclass, so reordering the two silently reclassifies every uninstalled
        # tree as a corrupt one and sends the operator to `fsck` instead of
        # `uv sync`. Pinned by
        # test_version_report.py::test_absent_distribution_resolves_to_the_sentinel.
        return ResolvedVersion.unresolvable(VersionSource.NOT_INSTALLED)
    except Exception as exc:
        # A deliberately broad catch (#296), defensible only because it reports
        # what it caught rather than swallowing it — `unreadable` below is that
        # report, and the module docstring is the rationale.
        #
        # This site carries no BLE001 suppression, and neither do most of its
        # siblings: of the 79 `except Exception` sites in `src/`, 14 carry the
        # directive and 65 do not. So there is no convention here to follow in
        # either direction — do not copy either shape from this site on the
        # assumption that it settles anything.
        #
        # Nor is the directive inert on principle. BLE001 is not in ruff 0.11's
        # default set (the version on this developer's PATH) but *is* from 0.16,
        # which this tree has also been run with — so whether the fourteen do
        # anything is a function of which ruff runs, and nothing pins one: there
        # is no ruff in `pyproject.toml`, none in `uv.lock`, no `[tool.ruff]`
        # section, and no lint step in either CI workflow. #285 (open) is what
        # decides whether ruff gates CI at all; revisit this comment with it.
        #
        # `Exception`, never `BaseException`: a Ctrl-C during a slow read on a
        # hung mount must interrupt the process, not be reported as a damaged
        # install and then hidden behind a version string.
        return ResolvedVersion.unreadable(exc)
    if not reported:
        return ResolvedVersion.unresolvable(VersionSource.METADATA_INCOMPLETE)
    return ResolvedVersion.installed(reported)


def unknown_version_diagnostic(
    source: VersionSource, *, detail: str | None
) -> str | None:
    """The operator-facing warning for `source`, or None when nothing is wrong.

    Pure (it can raise, but reads and writes nothing). Returns a multi-line
    string — cause, remedy, and, when the resolution swallowed an exception, a
    `cause:` line rendering it — for every source except `INSTALLED`. The detail
    is **appended** to the remedy, never substituted for it: the remedy is what
    an operator acts on, and on the `METADATA_UNREADABLE` path (where `detail` is
    always set) that would otherwise be the only line they ever see.

    The exhaustiveness that keeps a future cause from falling through to `None`,
    and so silently reinstating #291 for it, is enforced on `VersionSource`
    itself at class creation; this is the named concept the call site reads, not
    the guard. A healthy source arriving *with* a detail is the one pairing this
    cannot express, and it raises rather than discarding the exception — which
    is what it used to do.

    **`detail` is keyword-only with no default**, the shape #234 established for
    a parameter whose omission is silently wrong: it is the only channel by which
    the broad `except Exception` reports what it caught, so a call site that
    forgets it turns a reported catch back into a silent one. There is exactly
    one production call site — `localmail/__init__.py`, the only place that has
    both halves — which is what makes the requirement free rather than noisy.
    """
    remedy = source.diagnostic
    if remedy is None:
        if detail is not None:
            raise ValueError(
                f"{source.value} reports a healthy version but carries a "
                f"swallowed exception ({detail!r}); the two cannot both be true."
            )
        return None
    if detail is None:
        return remedy
    # Appended, never substituted: the remedy is the thing an operator acts on.
    return f"{remedy}\n{_CAUSE_PREFIX}{detail}"


#: Diagnostics already reported in this process. Process state, exactly like
#: `embed_worker._FAILURE_LOG`: `serve` reports at its own entry point *and*
#: inside `create_app`, and an operator does not need the same line twice.
#: Under `uvicorn --workers N` each worker is its own process, so each still
#: reports once — which is what a per-process record is for.
_REPORTED: set[str] = set()


def reset_version_reports() -> None:
    """Forget what this process has reported, so one test cannot silence the
    next one's line. Same shape as `embed_worker.reset_failure_log()`."""
    _REPORTED.clear()


def log_version_diagnostic(log: logging.Logger, diagnostic: str | None) -> None:
    """Report `diagnostic` once per process, or say nothing when there is none.

    The one rule for how a process surfaces an unresolvable version (#295),
    shared by `serve`, the daemon, and — since #304 — the `main` group callback
    on behalf of every other CLI command, so none of them can drift to a
    different level or wording. `--version` alone is deliberately not a caller:
    it writes to stderr through click, because its stdout is a machine-readable
    line that the manual's install-verification step parses.

    **ERROR, not WARNING.** `localmail run --log-level ERROR` is an offered
    `click.Choice`, and `run_cmd` calls `basicConfig` with it *before*
    constructing the daemon — so at WARNING this line was filtered out entirely,
    with `basicConfig`'s root handler also removing the `logging.lastResort`
    escape. A report the process can be told to discard is not a report. ERROR
    is the highest severity the CLI's `--log-level` can be set to, so it is the
    only level that clears every setting an operator can choose; the pinning
    test calls that the *quietest* setting, which is the same fact named from
    the operator's side rather than the record's. It costs one line at startup.

    **How the line reaches anyone differs by caller, and the wording is what
    covers the gap (#302).** `run_cmd` has called `basicConfig`, so its record is
    formatted like every other and is greppable by level. `serve` configures no
    logging at this point and nothing it imports does either, and neither does
    the group callback that reports for the remaining 36 commands — so those
    records go out through `logging.lastResort`: stderr, message only, no level,
    no timestamp, no logger name. All of them reach the operator; most are not
    greppable by level. That is why `_SEVERITY_PREFIX` exists and why it is
    derived from `_REPORT_LEVEL` rather than written beside it — on every
    unformatted path the word in the text is the only severity marker there is,
    so it must not contradict the record.

    Configuring logging here to close that was rejected: this function is called
    from a group callback that precedes all 38 commands, and installing a root
    handler for every one of them changes far more than the line it would
    format. The level still matters where it is not decoration — `run` after
    `basicConfig`, `create_app` under uvicorn's `dictConfig`, and any embedder
    that constructs `Daemon` directly.

    The falsy guard covers `""` as well as `None`: an empty diagnostic would
    otherwise emit a blank line, and a line that fires on every start is a line
    operators learn to skip.
    """
    if not diagnostic or diagnostic in _REPORTED:
        return
    _REPORTED.add(diagnostic)
    log.log(_REPORT_LEVEL, "%s", diagnostic)
