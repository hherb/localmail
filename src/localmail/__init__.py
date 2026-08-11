# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""localmail — local PostgreSQL archive of one or more IMAP accounts."""
from localmail.version_report import (
    ResolvedVersion as _ResolvedVersion,
    VersionSource as _VersionSource,
    resolve_version as _resolve_version,
    unknown_version_diagnostic as _unknown_version_diagnostic,
)

# `pyproject.toml` carries the only version literal in the Python tree. This
# reads it back from the installed distribution metadata rather than repeating
# it, so the two cannot disagree — and every other reader (`/v1/version`, the
# CLI's `--version`) goes through these attributes rather than looking it up
# again. Phrased as an invariant, not a count: the count was "the one other
# reader" until #279 added the second and did not update this line.
#
# Resolved exactly once, here, and exported as a pair: `__version__` for the
# readers that just want a string, and `__version_source__` for the one reader
# that has to explain a failure to a human. Re-deriving it per reader is the
# footgun a bare `@click.version_option()` carries — the same question asked
# twice, with different failure semantics.
#
# Note the metadata is stamped at *install* time: an editable tree whose
# pyproject was bumped without a re-sync reports the old version until the next
# `uv sync`/`uv run`. That is what `test_package_version_matches_pyproject`
# catches.
#
# Everything `version_report` exports is aliased private above and the resolved
# pair is deleted below, so `__version__` / `__version_source__` are the only
# public names this module adds. `localmail.resolve_version` would otherwise be
# a public second way to ask the same question — the very footgun the paragraph
# above is about.
_resolved: _ResolvedVersion = _resolve_version()

#: Always a non-empty str — `version_report.UNKNOWN_VERSION` when the metadata
#: could not be read. It matters downstream that this never becomes None: the
#: GUI's connect probe decodes `/v1/version`'s `server_version` as a
#: non-optional String and fails the whole trust flow on a null.
#: (`commands/version.rs` types the same field `Option<String>`; it is
#: `commands/connect.rs`'s probe, at trust time, that cannot take a null.)
__version__: str = _resolved.version

#: Why `__version__` is what it is, so the sentinel can be reported as a
#: failure instead of passing for an answer (#291). Not inferable from
#: `__version__` alone: the failure causes share one sentinel and have
#: different remedies.
__version_source__: _VersionSource = _resolved.source

#: The finished operator-facing warning, or None when the version is real.
#:
#: Rendered here rather than by each reader, and that is load-bearing (#295,
#: #296). There are three readers now — `--version`, `serve`, the daemon — and
#: the exception type behind a `METADATA_UNREADABLE` resolution is known *only*
#: at resolution time, so a reader handed just `__version_source__` would drop
#: it silently. Exporting the finished string makes that omission impossible
#: rather than merely discouraged, which is the same call `unknown_version_
#: diagnostic`'s keyword-only `detail` makes one layer down.
#:
#: Deliberately not on the wire: `/v1/version` keeps its three keys. The GUI's
#: connect probe is why the sentinel exists at all rather than a null, and a new
#: key nothing renders is #278 from the other end.
__version_diagnostic__: str | None = _unknown_version_diagnostic(
    _resolved.source, detail=_resolved.detail
)

del _resolved
