# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Which build is this process running, and why can we not say (#278, #300).

A sibling of `version_report.py`, and top-level for the same reason:
`serve/routes/version.py` reads it, and a subpackage would invite an import
cycle.

Four properties differ from `version_report`, each deliberately:

* **Nothing here logs, and no source carries a remedy.** `VersionSource`
  forces a remedy onto every failure member at class creation, because an
  unresolvable *version* is always a fault. An unresolvable *build hash*
  usually is not — `NOT_A_REPO` is the normal, correct state of an installed
  artifact — so copying that rule across would put an ERROR in front of an
  operator for a healthy install, which is #291 inverted.
* **Resolution is lazy and cached, never at import.** See `resolve_build_info`.
* **The repo we find must be *ours*.** See `_resolve_from_package_dir`.
* **The enum's own value IS the wire contract**, unlike `VersionSource`'s
  hyphenated debugging aids. Pinned literally in `tests/test_build_report.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BuildSource(Enum):
    """Where the build identity came from, or why there is none.

    **The value is the wire string** — parsed by clients, underscored to match
    this API's other wire enum (`rewrite_note_code`). Contrast `VersionSource`,
    whose values are debugging aids and which therefore carries a separate
    `wire_name`.
    """

    #: A generated `_build_info.py` was found. Nothing writes one today; this is
    #: the declared seam for a future build hook (see the spec's Out of scope).
    STAMPED = "stamped"
    #: Resolved from the working tree the imported package came from.
    GIT_CHECKOUT = "git_checkout"
    #: An installed artifact, or a repository that is not this project's.
    NOT_A_REPO = "not_a_repo"
    #: No `git` binary on PATH.
    GIT_UNAVAILABLE = "git_unavailable"
    #: git ran and failed, or timed out.
    GIT_FAILED = "git_failed"


#: The sources that carry no hash. `build_hash is None` iff `source` is one of
#: these — the biconditional `BuildInfo.__post_init__` enforces.
UNIDENTIFIED_SOURCES = frozenset({
    BuildSource.NOT_A_REPO,
    BuildSource.GIT_UNAVAILABLE,
    BuildSource.GIT_FAILED,
})


@dataclass(frozen=True)
class BuildInfo:
    """A build identity, or the named reason there is none."""

    #: "eec8e09" / "eec8e09-dirty", or None for an unidentified source.
    build_hash: str | None
    source: BuildSource

    def __post_init__(self) -> None:
        """Enforce the pairing the field comments would otherwise only claim.

        The `ResolvedVersion.__post_init__` shape. A blank hash is rejected as
        well as a missing one: `is not None` admits `""`, which renders as an
        empty "Server build" row — indistinguishable from a value withheld.
        """
        unidentified = self.source in UNIDENTIFIED_SOURCES
        if unidentified and self.build_hash is not None:
            raise ValueError(
                f"build_hash must be None for {self.source.value!r}, "
                f"got {self.build_hash!r}"
            )
        if not unidentified and not (self.build_hash or "").strip():
            raise ValueError(
                f"build_hash must be a non-blank string for "
                f"{self.source.value!r}, got {self.build_hash!r}"
            )
