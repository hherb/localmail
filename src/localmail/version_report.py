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

_NEVER_INSTALLED_REMEDY = (
    f"warning: the {DISTRIBUTION_NAME} version could not be determined — no "
    f"distribution metadata is installed for it here, so this is a source "
    f"tree that was never installed.\n"
    f"  remedy: run `uv sync` in a development checkout, or "
    f"`uv tool install {DISTRIBUTION_NAME}`."
)

_DAMAGED_INSTALL_REMEDY = (
    f"warning: the {DISTRIBUTION_NAME} version could not be determined — its "
    f"distribution metadata is installed but carries no version, so the "
    f"install is damaged.\n"
    f"  remedy: run `uv sync --reinstall-package {DISTRIBUTION_NAME}` in a "
    f"development checkout, or `uv tool install --reinstall "
    f"{DISTRIBUTION_NAME}`."
)

_UNREADABLE_METADATA_REMEDY = (
    f"warning: the {DISTRIBUTION_NAME} version could not be determined — "
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
_CAUSE_PREFIX = "  cause: "


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

    def __new__(cls, value: str, diagnostic: str | None) -> VersionSource:
        member = object.__new__(cls)
        member._value_ = value
        member.diagnostic = diagnostic
        return member

    #: The distribution metadata was read; `__version__` is real.
    INSTALLED = ("installed", None)
    #: No dist-info for this distribution. Note the src layout: a checkout that
    #: was never installed cannot be imported at all (`python -m localmail`
    #: from the repo root is a `ModuleNotFoundError`, so it never reaches this
    #: branch, and the 2B.4 supervisor launches `sys.executable -m localmail`
    #: against an interpreter where the package is installed). The reachable
    #: triggers are an import of the sources without their metadata
    #: (`PYTHONPATH=src`, a vendored copy) and a dist-info removed from under a
    #: live install by a partial sync.
    NOT_INSTALLED = ("not-installed", _NEVER_INSTALLED_REMEDY)
    #: A dist-info exists but its METADATA carries no usable `Version:` header
    #: — a truncated or hand-edited install.
    METADATA_INCOMPLETE = ("metadata-incomplete", _DAMAGED_INSTALL_REMEDY)
    #: The metadata read itself raised (#296) — a METADATA in another encoding
    #: or truncated mid-multibyte, or an `OSError` (EIO, stale NFS handle) from
    #: a network-mounted `site-packages`. Separate from `METADATA_INCOMPLETE`
    #: because the file was never read at all here, and a reinstall is the wrong
    #: first move when the filesystem is what is failing.
    METADATA_UNREADABLE = ("metadata-unreadable", _UNREADABLE_METADATA_REMEDY)


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
        clean up.
        """
        carries = self.detail is not None
        should = self.source is VersionSource.METADATA_UNREADABLE
        if carries is not should:
            raise ValueError(
                f"{self.source.value} resolutions must carry "
                f"{'a' if should else 'no'} detail; got {self.detail!r}"
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
        fails here (`failure_pacing.py`'s reason), so the type name always leads.
        """
        rendered = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return cls(UNKNOWN_VERSION, VersionSource.METADATA_UNREADABLE, rendered)


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
        # This site carries no BLE001 suppression while fourteen sibling broad
        # catches in `src/` do. That is a divergence, not a settled convention:
        # the rule is not in ruff's default set, so the directive is inert under
        # the pinned ruff — but it is live under newer ones, and #285 (still
        # open) is the issue that decides whether ruff gates CI at all. Revisit
        # both together rather than copying either shape from here.
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

    The one rule for how a long-running process surfaces an unresolvable version
    (#295), shared by `serve` and the daemon so the two cannot drift to different
    levels or wordings. The CLI is deliberately not a caller: `--version` writes
    to stderr through click, because its stdout is a machine-readable line that
    the manual's install-verification step parses.

    **ERROR, not WARNING.** `localmail run --log-level ERROR` is an offered
    `click.Choice`, and `run_cmd` calls `basicConfig` with it *before*
    constructing the daemon — so at WARNING this line was filtered out entirely,
    with `basicConfig`'s root handler also removing the `logging.lastResort`
    escape. A report the process can be told to discard is not a report, and
    ERROR is the loudest level that CLI offers. It costs one line at startup.

    The falsy guard covers `""` as well as `None`: an empty diagnostic would
    otherwise emit a blank line, and a line that fires on every start is a line
    operators learn to skip.
    """
    if not diagnostic or diagnostic in _REPORTED:
        return
    _REPORTED.add(diagnostic)
    log.error("%s", diagnostic)
