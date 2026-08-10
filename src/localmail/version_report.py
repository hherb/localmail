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

Everything here is pure except `resolve_version`, whose single side effect is
the metadata read.
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


class VersionSource(Enum):
    """Why `__version__` holds the value it does.

    Carried beside the version rather than inferred from it: a caller cannot
    tell the two failure causes apart from `UNKNOWN_VERSION` alone, and
    string-matching the sentinel to find out is how the remedies drift.
    """

    #: The distribution metadata was read; `__version__` is real.
    INSTALLED = "installed"
    #: No dist-info for this distribution — a source tree that was never
    #: installed. `python -m localmail` from a checkout is a first-class entry
    #: point (the 2B.4 supervisor launches the daemon that way), so this is the
    #: reachable case; `uv tool install` stamps metadata, so the manual's
    #: install-verification path does not normally land here.
    NOT_INSTALLED = "not-installed"
    #: A dist-info exists but its METADATA carries no usable `Version:` header
    #: — a truncated or hand-edited install.
    METADATA_INCOMPLETE = "metadata-incomplete"


@dataclass(frozen=True)
class ResolvedVersion:
    """The version string and the reason it is what it is."""

    version: str
    source: VersionSource


#: One remedy per unknown cause. `INSTALLED` is deliberately absent — its
#: lookup returning `None` is what makes "no diagnostic" the default, so a
#: healthy install can never emit a warning.
_DIAGNOSTICS: dict[VersionSource, str] = {
    VersionSource.NOT_INSTALLED: (
        f"warning: the {DISTRIBUTION_NAME} version could not be determined — no "
        f"distribution metadata is installed for it here, so this is a source "
        f"tree that was never installed.\n"
        f"  remedy: run `uv sync` in a development checkout, or "
        f"`uv tool install {DISTRIBUTION_NAME}`."
    ),
    VersionSource.METADATA_INCOMPLETE: (
        f"warning: the {DISTRIBUTION_NAME} version could not be determined — its "
        f"distribution metadata is installed but carries no version, so the "
        f"install is damaged.\n"
        f"  remedy: run `uv sync --reinstall-package {DISTRIBUTION_NAME}` in a "
        f"development checkout, or `uv tool install --reinstall "
        f"{DISTRIBUTION_NAME}`."
    ),
}


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
        return ResolvedVersion(UNKNOWN_VERSION, VersionSource.NOT_INSTALLED)
    if not reported:
        return ResolvedVersion(UNKNOWN_VERSION, VersionSource.METADATA_INCOMPLETE)
    return ResolvedVersion(reported, VersionSource.INSTALLED)


def unknown_version_diagnostic(source: VersionSource) -> str | None:
    """The operator-facing warning for `source`, or None when nothing is wrong.

    Pure. Returns a multi-line string (cause, then remedy) for every cause
    except `INSTALLED`; `test_every_unknown_source_has_a_diagnostic` is what
    stops a future cause from silently reinstating #291 by falling through to
    `None`.
    """
    return _DIAGNOSTICS.get(source)
