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

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path


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
    #: An installed artifact, or a repository that is not this project's. git
    #: exits 128 here for several causes we deliberately do not separate: no
    #: repository (the common one), dubious ownership, a repo with no commits.
    #: Telling them apart would mean matching git's stderr text, which this
    #: codebase forbids on principle — match the type, never the message.
    NOT_A_REPO = "not_a_repo"
    #: No `git` binary on PATH.
    GIT_UNAVAILABLE = "git_unavailable"
    #: git raised, timed out, or answered in a shape we could not parse. Note a
    #: non-zero exit from the first probe routes to NOT_A_REPO, not here.
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


#: Bounds each git call. Worst case is two timeouts, paid once per process on a
#: path no request blocks behind twice. Generous for `rev-parse` and
#: `diff --quiet` on a repository of this size (both are milliseconds warm) and
#: short enough that a wedged mount does not hold the first `/v1/version` open.
_GIT_TIMEOUT_S = 2.0

#: Stripped from the subprocess environment. A stray one makes `-C` a no-op, so
#: we would report an unrelated repository's SHA — cheap to prevent, and
#: invisible if it ever happened.
_STRIPPED_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE")

_DIRTY_SUFFIX = "-dirty"


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command. `argv` list, never a shell."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_GIT_ENV}
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        env=env,
        check=False,
    )


def _resolve_from_package_dir(package_dir: Path) -> BuildInfo:
    """Resolve the build identity for the package installed at `package_dir`.

    Takes the directory as a parameter rather than reading `__file__` so the
    identity guard is testable against a contrived layout.

    Never raises: every failure maps to a `BuildSource`. The broad catch is
    justified as `version_report`'s is — this feeds an endpoint that must
    answer — with one honest difference: not being on the import path, a raise
    here would not kill `import localmail`, only 500 an unauthenticated route.
    """
    try:
        probe = _run_git(package_dir, "rev-parse", "--show-toplevel", "--short", "HEAD")
    except FileNotFoundError:
        return BuildInfo(build_hash=None, source=BuildSource.GIT_UNAVAILABLE)
    except Exception:
        return BuildInfo(build_hash=None, source=BuildSource.GIT_FAILED)

    if probe.returncode != 0:
        return BuildInfo(build_hash=None, source=BuildSource.NOT_A_REPO)
    # `splitlines()`, never `.split()`: the toplevel is a path, and one
    # containing a space would otherwise yield 3+ tokens and report a
    # healthy checkout as GIT_FAILED.
    lines = probe.stdout.strip().splitlines()
    if len(lines) != 2:
        return BuildInfo(build_hash=None, source=BuildSource.GIT_FAILED)
    toplevel, short_sha = Path(lines[0]), lines[1]

    if not short_sha:
        # Unreachable through `splitlines()` today, and guarded anyway: what
        # makes it unreachable is a property of `str.strip`/`str.splitlines`
        # this module does not own, and the construction below is the one
        # unguarded call in a function whose contract is that it never raises.
        return BuildInfo(build_hash=None, source=BuildSource.GIT_FAILED)

    if not _repo_is_ours(toplevel, package_dir):
        return BuildInfo(build_hash=None, source=BuildSource.NOT_A_REPO)

    try:
        dirty = _run_git(package_dir, "diff", "--quiet", "HEAD")
    except Exception:
        return BuildInfo(build_hash=None, source=BuildSource.GIT_FAILED)
    # 0 = clean, 1 = tracked changes. Anything else is git failing, not a verdict.
    if dirty.returncode not in (0, 1):
        return BuildInfo(build_hash=None, source=BuildSource.GIT_FAILED)
    suffix = _DIRTY_SUFFIX if dirty.returncode == 1 else ""
    return BuildInfo(build_hash=f"{short_sha}{suffix}", source=BuildSource.GIT_CHECKOUT)


def _repo_is_ours(toplevel: Path, package_dir: Path) -> bool:
    """Does the repository git reported hold *this* package at its own path?

    `git rev-parse` run from inside a `site-packages` that happens to sit under
    an unrelated repository — a virtualenv inside a dotfiles repo — answers with
    that project's toplevel. Requiring `<toplevel>/src/localmail/__init__.py` to
    be the very file we imported is what tells the two apart; mere containment
    does not, because the installed copy *is* contained.
    """
    try:
        ours = (package_dir / "__init__.py").resolve()
        candidate = (toplevel / "src" / package_dir.name / "__init__.py").resolve()
    except OSError:
        return False
    return candidate == ours and candidate.exists()


@cache
def resolve_build_info() -> BuildInfo:
    """The build identity of the package this process imported.

    **Lazy and cached, never resolved at import.** `import localmail` runs for
    all 38 CLI commands; a `git` subprocess on that path costs every invocation
    and can *hang* — a stale network mount is the precise scenario #296 was
    about, and that module's first rule is that import must not fail.

    Caching also gives the semantics the "Server build" row wants: a value
    pinned for the life of the process, so it reports what the process is
    **running** rather than what the tree says now. That distinction is live on
    an editable install, where a `git pull` moves the tree under a daemon that
    keeps executing the code it already imported.

    `functools.cache` is not atomic, so a burst of concurrent first calls can
    each resolve once — bounded by the threadpool and by the 2s timeout, after
    which the cache is warm. Not worth a lock: the window is one cold start.
    """
    return _resolve_from_package_dir(Path(__file__).resolve().parent)


def reset_build_info() -> None:
    """Clear the process-wide cache. For tests; see the autouse conftest fixture."""
    resolve_build_info.cache_clear()
