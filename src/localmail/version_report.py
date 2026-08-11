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

**The two failure causes are kept apart because their remedies differ**, which
is the only reason to read the line at all: nothing is installed, so install
something; or a dist-info is present but carries no `Version:` header, so
replace what is there. `uv sync` does not repair the second, and `--reinstall`
is wasted on the first. They used to collapse to one string.

Pure except `resolve_version`, whose one impure step is the metadata read.
"""
from __future__ import annotations

import importlib.metadata
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


@dataclass(frozen=True)
class ResolvedVersion:
    """The version string and the reason it is what it is."""

    version: str
    source: VersionSource

    @classmethod
    def installed(cls, version: str) -> ResolvedVersion:
        return cls(version, VersionSource.INSTALLED)

    @classmethod
    def unresolvable(cls, source: VersionSource) -> ResolvedVersion:
        """The sentinel is supplied here, so no caller can pair a real version
        with a failure cause — or spell `UNKNOWN_VERSION` a second time."""
        return cls(UNKNOWN_VERSION, source)


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
        return ResolvedVersion.unresolvable(VersionSource.NOT_INSTALLED)
    if not reported:
        return ResolvedVersion.unresolvable(VersionSource.METADATA_INCOMPLETE)
    return ResolvedVersion.installed(reported)


def unknown_version_diagnostic(source: VersionSource) -> str | None:
    """The operator-facing warning for `source`, or None when nothing is wrong.

    Pure. Returns a multi-line string (cause, then remedy) for every cause
    except `INSTALLED`. The exhaustiveness that keeps a future cause from
    falling through to `None` — and so silently reinstating #291 for it — is
    enforced on `VersionSource` itself, at class creation; this is the named
    concept the call site reads, not the guard.
    """
    return source.diagnostic
