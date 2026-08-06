# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""localmail — local PostgreSQL archive of one or more IMAP accounts."""
from importlib.metadata import PackageNotFoundError, version as _package_version

# `pyproject.toml` carries the only version literal in the Python tree. This
# reads it back from the installed distribution metadata rather than repeating
# it, so the two cannot disagree — and every other reader (notably
# `/v1/version`) goes through this attribute rather than looking it up again.
try:
    __version__ = _package_version("localmail")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
