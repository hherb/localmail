# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""localmail — local PostgreSQL archive of one or more IMAP accounts."""
from importlib.metadata import PackageNotFoundError, version as _package_version

# `pyproject.toml` carries the only version literal in the Python tree. This
# reads it back from the installed distribution metadata rather than repeating
# it, so the two cannot disagree — and `/v1/version`, the one other reader,
# goes through this attribute rather than looking it up again.
#
# Note the metadata is stamped at *install* time: an editable tree whose
# pyproject was bumped without a re-sync reports the old version until the next
# `uv sync`/`uv run`. That is what `test_package_version_matches_pyproject`
# catches.
try:
    # `version()` returns None — it does not raise — when the dist-info exists
    # but its METADATA carries no `Version:` header (a truncated install, a
    # hand-edited dist-info). typeshed declares it `-> str`, so mypy cannot
    # catch that; the `or` is what keeps `__version__` a str for every reader.
    # It matters downstream: the GUI's connect probe decodes `server_version`
    # as a non-optional String and fails the whole trust flow on a null.
    __version__ = _package_version("localmail") or "0.0.0+unknown"
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
