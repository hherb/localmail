# Build Provenance On The Wire — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/v1/version` reports which build the server is running and why it cannot say, closing #278 (the About tab renders a `build_hash` nothing emits) and #300 (an unresolvable version has no machine-readable channel).

**Architecture:** A new lazily-resolved, process-cached `build_report.py` derives a build identity from the git checkout the imported package came from, mapping every failure to a named source rather than to a bare `null`. `/v1/version` gains `build_hash`, `build_source` and `version_source`; the desktop GUI renders the hash it already declares and explains an absent one.

**Tech Stack:** Python ≥3.13 (`.python-version` is 3.13; CI matrixes 3.12 + 3.13), `subprocess` against the `git` binary, FastAPI, pytest. GUI: Rust (serde) + Svelte 5 + vitest.

**Spec:** [docs/superpowers/specs/2026-08-15-build-provenance-design.md](../specs/2026-08-15-build-provenance-design.md)

## Global Constraints

- **`import localmail` must never fail.** Nothing in this plan runs at import time; `resolve_build_info()` is lazy and cached. A `git` subprocess on the import path would cost all 38 CLI commands and can hang on a stale mount — the scenario #296 exists for.
- **`resolve_build_info()` never raises.** Every failure maps to a `BuildSource`.
- **No shell.** Every subprocess call is an `argv` list with `shell=False` (the default). Never build a command string.
- **`_GIT_TIMEOUT_S = 2.0`** bounds each git call.
- **Dirtiness is measured on tracked files only** (`git diff --quiet HEAD`). Untracked files are excluded: scratch files would make every deployment read dirty forever, and a marker that is always on carries no information.
- **The diagnostic *text* never goes on the wire.** `/v1/version` is unauthenticated and `__version_diagnostic__` embeds rendered exception text carrying errno values and filesystem paths (#303). Identifiers yes; paths and exception strings no.
- **Wire strings are declared, never derived from a member name.** `VersionSource`'s own values are hyphenated debugging aids (`"not-installed"`); this API's wire enums are underscored (`rewrite_note_code` ships `not_configured`, `continuation_page`). Deriving would both break the convention and let a rename silently change the contract.
- **Exact wire values.** `build_source` ∈ {`stamped`, `git_checkout`, `not_a_repo`, `git_unavailable`, `git_failed`}. `version_source` ∈ {`installed`, `not_installed`, `metadata_incomplete`, `metadata_unreadable`}. Both are **always present and never null**; only `build_hash` is nullable.
- **No new dependency, no migration, no config key.**
- **Branch + PR.** All work lands on one branch through one PR (CLAUDE.md `## Conventions`). Never push to `main`.
- **Commands** are run as `unset VIRTUAL_ENV && uv run …` (a stray `VIRTUAL_ENV` makes `uv` pick the wrong interpreter).

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/localmail/build_report.py` (create) | `BuildSource`, `BuildInfo`, git resolution, `resolve_build_info()`, `reset_build_info()`. Top-level, sibling of `version_report.py`, because `serve/routes/version.py` reads it and a subpackage invites an import cycle. |
| `tests/test_build_report.py` (create) | Pure invariants, real-temp-repo resolution, the identity guard, injected failures, resolve-once. |
| `src/localmail/version_report.py` (modify) | `VersionSource` gains a `wire_name` member payload + `reject_empty_wire_name`. |
| `tests/test_version_report.py` (modify) | Wire-name literals and the class-creation rejection. |
| `tests/conftest.py` (modify) | Autouse `reset_build_info()` fixture. |
| `src/localmail/serve/routes/version.py` (modify) | Emit the three new keys. |
| `tests/test_serve_version_route.py` (create) | The wire contract, including the no-diagnostic-text assertion. |
| `tests/test_cli_version_stderr_contract.py` (create) | #300's CLI half: stderr non-empty ⟺ unresolvable. |
| `gui/src-tauri/src/commands/version.rs` (modify) | `VersionInfo` gains two `Option<String>` fields. |
| `gui/src/lib/api/version.ts` (modify) | `ServerVersionInfo` gains the same two. |
| `gui/src/lib/build_provenance.ts` (create) | Pure: source string → human phrase. Logic out of components, per project convention. |
| `gui/src/lib/build_provenance.test.ts` (create) | Unit tests for the phrase mapping. |
| `gui/src/screens/settings/SettingsAbout.svelte` (modify) | Render the hash, explain an absent one, mark a non-`installed` version source. |
| `README.md`, `CLAUDE.md` (modify) | The wire contract and the module's rules. |

---

### Task 1: `BuildSource` and `BuildInfo` — the pure core

**Files:**
- Create: `src/localmail/build_report.py`
- Test: `tests/test_build_report.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `BuildSource` (Enum, `.value` is the wire string), `UNIDENTIFIED_SOURCES: frozenset[BuildSource]`, `BuildInfo(build_hash: str | None, source: BuildSource)` — a frozen dataclass whose `__post_init__` raises `ValueError` on a mismatched pairing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_build_report.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Build provenance: the value, and why it is what it is (#278).

`build_hash` is worthless without a way to say why it is absent — "installed
from a wheel" (normal) and "git ran and failed" (notable) are the same `null`
otherwise, which is the shape #291 spent four sessions removing from the
version line.
"""
from __future__ import annotations

import pytest

from localmail.build_report import (
    UNIDENTIFIED_SOURCES, BuildInfo, BuildSource,
)


def test_the_wire_values_are_the_contract_and_are_asserted_literally() -> None:
    """These strings are parsed by clients, so a rename must fail here.

    Underscored, matching this API's wire-enum precedent (`rewrite_note_code`
    ships `not_configured`), and deliberately unlike `VersionSource`, whose
    hyphenated values CLAUDE.md documents as debugging aids rather than wire.
    """
    assert BuildSource.STAMPED.value == "stamped"
    assert BuildSource.GIT_CHECKOUT.value == "git_checkout"
    assert BuildSource.NOT_A_REPO.value == "not_a_repo"
    assert BuildSource.GIT_UNAVAILABLE.value == "git_unavailable"
    assert BuildSource.GIT_FAILED.value == "git_failed"


def test_every_source_is_classified_exactly_once() -> None:
    """The partition the `__post_init__` biconditional pivots on."""
    identified = set(BuildSource) - UNIDENTIFIED_SOURCES
    assert identified == {BuildSource.STAMPED, BuildSource.GIT_CHECKOUT}
    assert UNIDENTIFIED_SOURCES == {
        BuildSource.NOT_A_REPO,
        BuildSource.GIT_UNAVAILABLE,
        BuildSource.GIT_FAILED,
    }


@pytest.mark.parametrize("source", sorted(UNIDENTIFIED_SOURCES, key=lambda s: s.value))
def test_an_unidentified_source_may_not_carry_a_hash(source: BuildSource) -> None:
    with pytest.raises(ValueError, match="build_hash"):
        BuildInfo(build_hash="eec8e09", source=source)


@pytest.mark.parametrize("source", [BuildSource.STAMPED, BuildSource.GIT_CHECKOUT])
@pytest.mark.parametrize("hash_value", [None, "", "   "])
def test_an_identified_source_must_carry_a_real_hash(
    source: BuildSource, hash_value: str | None
) -> None:
    """Blank is rejected, not just None.

    `is not None` admits `""`, which renders as an empty 'Server build' row —
    the 'reads as if a value were withheld' outcome the guard exists to stop,
    the same call `ResolvedVersion.__post_init__` makes for a blank detail.
    """
    with pytest.raises(ValueError, match="build_hash"):
        BuildInfo(build_hash=hash_value, source=source)


def test_the_two_legal_shapes_construct() -> None:
    assert BuildInfo(build_hash="eec8e09", source=BuildSource.GIT_CHECKOUT).build_hash
    assert BuildInfo(build_hash=None, source=BuildSource.NOT_A_REPO).build_hash is None


def test_build_info_is_frozen() -> None:
    info = BuildInfo(build_hash="eec8e09", source=BuildSource.GIT_CHECKOUT)
    with pytest.raises(Exception):
        info.build_hash = "other"  # type: ignore[misc]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_build_report.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'localmail.build_report'`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/localmail/build_report.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_build_report.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/build_report.py tests/test_build_report.py
git commit -m "feat(build-report): name the build identity and why it may be absent (#278)"
```

---

### Task 2: Resolve from the git checkout

**Files:**
- Modify: `src/localmail/build_report.py`
- Test: `tests/test_build_report.py`

**Interfaces:**
- Consumes: `BuildInfo`, `BuildSource` from Task 1.
- Produces: `_resolve_from_package_dir(package_dir: Path) -> BuildInfo` — takes the directory as a **parameter** so the guards in Task 3 are testable without monkeypatching `__file__`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_build_report.py`:

```python
import shutil
import subprocess
from pathlib import Path

from localmail.build_report import _resolve_from_package_dir

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not installed"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    """A repo laid out like this project: <root>/src/localmail/__init__.py."""
    package_dir = tmp_path / "src" / "localmail"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("__version__ = '0.0.0'\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")
    return package_dir


@requires_git
def test_a_clean_checkout_resolves_to_its_short_sha(tmp_path: Path) -> None:
    package_dir = _make_repo(tmp_path)

    info = _resolve_from_package_dir(package_dir)

    assert info.source is BuildSource.GIT_CHECKOUT
    assert info.build_hash is not None
    assert not info.build_hash.endswith("-dirty")
    # The short SHA git itself reports, not a slice we computed.
    expected = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--short", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert info.build_hash == expected


@requires_git
def test_a_modified_tracked_file_makes_it_dirty(tmp_path: Path) -> None:
    package_dir = _make_repo(tmp_path)
    (package_dir / "__init__.py").write_text("__version__ = '9.9.9'\n")

    info = _resolve_from_package_dir(package_dir)

    assert info.source is BuildSource.GIT_CHECKOUT
    assert info.build_hash.endswith("-dirty")


@requires_git
def test_an_untracked_file_does_not(tmp_path: Path) -> None:
    """Tracked files only.

    Every working session leaves scratch files; a marker that is always on
    carries no information, which would make it decoration rather than the
    warning it exists to be.
    """
    package_dir = _make_repo(tmp_path)
    (tmp_path / "scratch.txt").write_text("notes\n")

    info = _resolve_from_package_dir(package_dir)

    assert not info.build_hash.endswith("-dirty")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_build_report.py -q -k "checkout or dirty or untracked"`
Expected: `ImportError: cannot import name '_resolve_from_package_dir'`.

- [ ] **Step 3: Write the minimal implementation**

Add to `src/localmail/build_report.py` (imports at the top of the file):

```python
import os
import subprocess
from pathlib import Path
```

and after `BuildInfo`:

```python
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
    """
    probe = _run_git(package_dir, "rev-parse", "--show-toplevel", "--short", "HEAD")
    if probe.returncode != 0:
        return BuildInfo(build_hash=None, source=BuildSource.NOT_A_REPO)
    lines = probe.stdout.split()
    if len(lines) != 2:
        return BuildInfo(build_hash=None, source=BuildSource.GIT_FAILED)
    toplevel, short_sha = Path(lines[0]), lines[1]

    dirty = _run_git(package_dir, "diff", "--quiet", "HEAD")
    # 0 = clean, 1 = tracked changes. Anything else is git failing, not a verdict.
    if dirty.returncode not in (0, 1):
        return BuildInfo(build_hash=None, source=BuildSource.GIT_FAILED)
    suffix = _DIRTY_SUFFIX if dirty.returncode == 1 else ""
    return BuildInfo(build_hash=f"{short_sha}{suffix}", source=BuildSource.GIT_CHECKOUT)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_build_report.py -q`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/build_report.py tests/test_build_report.py
git commit -m "feat(build-report): resolve the short SHA and a tracked-files dirty marker (#278)"
```

---

### Task 3: The guards — identity, and every failure named

**Files:**
- Modify: `src/localmail/build_report.py`
- Test: `tests/test_build_report.py`

**Interfaces:**
- Consumes: `_resolve_from_package_dir` from Task 2.
- Produces: no new public names; `_resolve_from_package_dir` gains the identity guard and the `FileNotFoundError` / `TimeoutExpired` mapping.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_build_report.py`:

```python
@requires_git
def test_a_repo_that_is_not_ours_is_not_our_build(tmp_path: Path) -> None:
    """The guard that fails silently if it is wrong, so it gets its own test.

    A virtualenv inside an unrelated git repository — a dotfiles repo, say —
    would otherwise have us report that project's SHA as localmail's build.
    """
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("someone else's repo\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")
    # An installed copy that happens to sit inside that repo.
    package_dir = tmp_path / ".venv" / "lib" / "site-packages" / "localmail"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("__version__ = '0.3.0'\n")

    info = _resolve_from_package_dir(package_dir)

    assert info.source is BuildSource.NOT_A_REPO
    assert info.build_hash is None


def test_a_missing_git_binary_is_named_not_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr("localmail.build_report.subprocess.run", boom)

    info = _resolve_from_package_dir(tmp_path)

    assert info.source is BuildSource.GIT_UNAVAILABLE
    assert info.build_hash is None


def test_a_git_timeout_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr("localmail.build_report.subprocess.run", hang)

    info = _resolve_from_package_dir(tmp_path)

    assert info.source is BuildSource.GIT_FAILED
    assert info.build_hash is None


def test_the_probe_strips_inherited_git_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray GIT_DIR makes `-C` a no-op, pointing us at another repo."""
    seen: dict[str, str] = {}

    def capture(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, returncode=128, stdout="", stderr="")

    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/somewhere/else")
    monkeypatch.setattr("localmail.build_report.subprocess.run", capture)

    _resolve_from_package_dir(tmp_path)

    assert "GIT_DIR" not in seen
    assert "GIT_WORK_TREE" not in seen


def test_resolution_never_raises_whatever_git_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module's first rule: this feeds an endpoint that must answer."""
    def wild(*_args, **_kwargs):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr("localmail.build_report.subprocess.run", wild)

    info = _resolve_from_package_dir(tmp_path)

    assert info.source is BuildSource.GIT_FAILED
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_build_report.py -q -k "not_ours or missing_git or timeout or strips or never_raises"`
Expected: FAIL — `test_a_repo_that_is_not_ours_is_not_our_build` reports `GIT_CHECKOUT`; the others raise `FileNotFoundError` / `TimeoutExpired` / `RuntimeError` out of the call.

- [ ] **Step 3: Write the minimal implementation**

Replace `_resolve_from_package_dir` in `src/localmail/build_report.py`:

```python
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
    lines = probe.stdout.split()
    if len(lines) != 2:
        return BuildInfo(build_hash=None, source=BuildSource.GIT_FAILED)
    toplevel, short_sha = Path(lines[0]), lines[1]

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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_build_report.py -q`
Expected: PASS (18 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/build_report.py tests/test_build_report.py
git commit -m "feat(build-report): name every failure, and refuse a repository that is not ours (#278)"
```

---

### Task 4: Lazy, process-cached resolution

**Files:**
- Modify: `src/localmail/build_report.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_build_report.py`

**Interfaces:**
- Consumes: `_resolve_from_package_dir` from Task 3.
- Produces: `resolve_build_info() -> BuildInfo` (public, cached) and `reset_build_info() -> None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_build_report.py`:

```python
from localmail.build_report import reset_build_info, resolve_build_info


def test_resolution_happens_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinned for the process, which is the semantics the row wants.

    An editable install's tree can move under a running daemon, so a value
    re-read per request would report the tree rather than what the process is
    actually running.
    """
    calls = []

    def counting(package_dir):
        calls.append(package_dir)
        return BuildInfo(build_hash="eec8e09", source=BuildSource.GIT_CHECKOUT)

    monkeypatch.setattr(
        "localmail.build_report._resolve_from_package_dir", counting
    )
    reset_build_info()

    first, second = resolve_build_info(), resolve_build_info()

    assert first is second
    assert len(calls) == 1


def test_it_asks_about_the_directory_the_package_was_imported_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never the process's working directory, which for a daemon is arbitrary."""
    import localmail

    seen = []

    def capture(package_dir):
        seen.append(package_dir)
        return BuildInfo(build_hash=None, source=BuildSource.NOT_A_REPO)

    monkeypatch.setattr("localmail.build_report._resolve_from_package_dir", capture)
    reset_build_info()

    resolve_build_info()

    assert seen == [Path(localmail.__file__).resolve().parent]


def test_reset_clears_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    infos = [
        BuildInfo(build_hash="aaaaaaa", source=BuildSource.GIT_CHECKOUT),
        BuildInfo(build_hash="bbbbbbb", source=BuildSource.GIT_CHECKOUT),
    ]
    monkeypatch.setattr(
        "localmail.build_report._resolve_from_package_dir",
        lambda _dir: infos.pop(0),
    )
    reset_build_info()
    assert resolve_build_info().build_hash == "aaaaaaa"

    reset_build_info()

    assert resolve_build_info().build_hash == "bbbbbbb"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_build_report.py -q -k "once_per_process or imported_from or reset_clears"`
Expected: `ImportError: cannot import name 'reset_build_info'`.

- [ ] **Step 3: Write the minimal implementation**

Add `from functools import cache` to the imports, and append to `src/localmail/build_report.py`:

```python
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
    """
    return _resolve_from_package_dir(Path(__file__).resolve().parent)


def reset_build_info() -> None:
    """Clear the process-wide cache. For tests; see the autouse conftest fixture."""
    resolve_build_info.cache_clear()
```

- [ ] **Step 4: Add the autouse fixture**

In `tests/conftest.py`, after the `fresh_version_reports` fixture:

```python
@pytest.fixture(autouse=True)
def fresh_build_info():
    """Clear the process-wide build-identity cache between tests (#278).

    `resolve_build_info` caches for the life of the process — correct in
    production, where the answer must not change under a running daemon, and
    wrong across tests, where one test's monkeypatched resolver would otherwise
    be the answer every later test sees. The `fresh_version_reports` shape.
    """
    from localmail.build_report import reset_build_info

    reset_build_info()
    yield
    reset_build_info()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_build_report.py -q`
Expected: PASS (21 tests).

- [ ] **Step 6: Commit**

```bash
git add src/localmail/build_report.py tests/test_build_report.py tests/conftest.py
git commit -m "feat(build-report): resolve lazily, once per process (#278)"
```

---

### Task 5: `VersionSource` gains a declared wire name

**Files:**
- Modify: `src/localmail/version_report.py`
- Test: `tests/test_version_report.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `VersionSource.wire_name: str` on every member, and module-level `reject_empty_wire_name(value: str, wire_name: str) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_version_report.py`:

```python
def test_the_wire_names_are_declared_and_asserted_literally() -> None:
    """These strings are parsed by clients (#300), so a rename must fail here.

    Note they are NOT the member values, which are hyphenated debugging aids
    this module's own docstring says are not a wire contract. This API's wire
    enums are underscored — `rewrite_note_code` ships `not_configured` — so
    deriving the wire from the value would break the convention *and* let a
    rename change the contract silently.
    """
    assert VersionSource.INSTALLED.wire_name == "installed"
    assert VersionSource.NOT_INSTALLED.wire_name == "not_installed"
    assert VersionSource.METADATA_INCOMPLETE.wire_name == "metadata_incomplete"
    assert VersionSource.METADATA_UNREADABLE.wire_name == "metadata_unreadable"


def test_the_wire_name_is_not_the_member_value() -> None:
    """The concrete demonstration of why it is declared rather than derived."""
    assert VersionSource.NOT_INSTALLED.value == "not-installed"
    assert VersionSource.NOT_INSTALLED.wire_name == "not_installed"


def test_every_member_has_a_wire_name() -> None:
    names = [s.wire_name for s in VersionSource]
    assert all(names)
    assert len(set(names)) == len(names), "wire names must be unique"


@pytest.mark.parametrize("bad", ["", "   "])
def test_an_empty_wire_name_is_rejected_at_class_creation(bad: str) -> None:
    """A member written `("x", None, "")` satisfies the signature, so no
    `TypeError` fires — and the route would then emit an empty string for a
    real source, which is #291 one level up.

    Module-level rather than inline for the reason `reject_empty_diagnostic` is:
    enum machinery replaces `__new__` after class creation, so no test can reach
    the production one to prove the rule fires for a *future* member.
    """
    from localmail.version_report import reject_empty_wire_name

    with pytest.raises(ValueError, match="wire_name"):
        reject_empty_wire_name("some-source", bad)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_version_report.py -q -k "wire_name"`
Expected: FAIL — `AttributeError: 'VersionSource' object has no attribute 'wire_name'`.

- [ ] **Step 3: Write the minimal implementation**

In `src/localmail/version_report.py`, add beside `reject_empty_diagnostic`:

```python
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
    """
    if not wire_name.strip():
        raise ValueError(f"VersionSource {value!r} declares an empty wire_name")
    return wire_name
```

Then change the class body:

```python
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
```

and give every member its third element, leaving the existing comments in place:

```python
    INSTALLED = ("installed", None, "installed")
    NOT_INSTALLED = ("not-installed", _NEVER_INSTALLED_REMEDY, "not_installed")
    METADATA_INCOMPLETE = (
        "metadata-incomplete", _DAMAGED_INSTALL_REMEDY, "metadata_incomplete",
    )
    METADATA_UNREADABLE = (
        "metadata-unreadable", _UNREADABLE_METADATA_REMEDY, "metadata_unreadable",
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_version_report.py tests/test_version_startup_report.py tests/test_version_single_source.py -q`
Expected: PASS — the whole version cluster, since `__new__`'s signature changed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/version_report.py tests/test_version_report.py
git commit -m "feat(version-report): declare each source's wire name (#300)"
```

---

### Task 6: `/v1/version` emits the three keys

**Files:**
- Modify: `src/localmail/serve/routes/version.py`
- Create: `tests/test_serve_version_route.py`

**Interfaces:**
- Consumes: `resolve_build_info`, `BuildSource` (Task 4); `VersionSource.wire_name` (Task 5).
- Produces: the wire contract other clients read.

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_version_route.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The /v1/version wire contract (#278, #300).

`build_hash` alone cannot say why it is absent, and "why is this value absent"
is exactly #300's question about `server_version`. Both are answered by a
declared source string rather than by a bare null.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import localmail.serve.routes.version as version_route
from localmail.build_report import BuildInfo, BuildSource
from localmail.serve.app import create_app
from localmail.version_report import VersionSource


def _client(db_dsn: str) -> TestClient:
    return TestClient(create_app(dsn=db_dsn, state_signing_key="k" * 32))


def test_the_six_keys_are_present(db_dsn: str) -> None:
    body = _client(db_dsn).get("/v1/version").json()
    assert set(body) == {
        "api_major", "api_minor", "server_version",
        "build_hash", "build_source", "version_source",
    }


def test_the_source_fields_are_never_null(db_dsn: str) -> None:
    """Only `build_hash` is nullable. A client that cannot explain an absent
    hash is the state this design exists to end."""
    body = _client(db_dsn).get("/v1/version").json()
    assert isinstance(body["build_source"], str) and body["build_source"]
    assert isinstance(body["version_source"], str) and body["version_source"]


def test_an_identified_build_reports_its_hash(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        version_route, "resolve_build_info",
        lambda: BuildInfo(build_hash="eec8e09-dirty", source=BuildSource.GIT_CHECKOUT),
    )
    body = _client(db_dsn).get("/v1/version").json()
    assert body["build_hash"] == "eec8e09-dirty"
    assert body["build_source"] == "git_checkout"


def test_an_unidentified_build_names_the_reason_rather_than_only_nulling(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        version_route, "resolve_build_info",
        lambda: BuildInfo(build_hash=None, source=BuildSource.NOT_A_REPO),
    )
    body = _client(db_dsn).get("/v1/version").json()
    assert body["build_hash"] is None
    assert body["build_source"] == "not_a_repo"


def test_an_unresolvable_version_is_flagged_on_the_wire(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#300: the sentinel used to ship unflagged, and the GUI rendered it."""
    monkeypatch.setattr(
        version_route, "VERSION_SOURCE", VersionSource.METADATA_UNREADABLE
    )
    body = _client(db_dsn).get("/v1/version").json()
    assert body["version_source"] == "metadata_unreadable"


def test_the_diagnostic_text_never_reaches_the_unauthenticated_body(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint is unauthenticated and the diagnostic embeds rendered
    exception text — errno values and filesystem paths since #303.

    Asserted against the module's own constants with a positive control beside
    them: `"cause:" not in body` cannot fail once the prefix is renamed.
    """
    from localmail.version_report import _CAUSE_PREFIX, _SEVERITY_PREFIX

    monkeypatch.setattr(
        version_route, "VERSION_SOURCE", VersionSource.METADATA_UNREADABLE
    )
    raw = _client(db_dsn).get("/v1/version").text

    assert _CAUSE_PREFIX not in raw
    assert _SEVERITY_PREFIX not in raw
    # Positive control: the source that would carry them IS what we are reporting.
    assert "metadata_unreadable" in raw
```

> Both constants exist today: `_SEVERITY_PREFIX` at `version_report.py:105`
> (`f"{logging.getLevelName(_REPORT_LEVEL).lower()}: "`) and `_CAUSE_PREFIX` at
> `version_report.py:141` (`"  cause: "`). Import them; never inline the
> strings, which is what would make the assertion unfalsifiable once either is
> renamed.

- [ ] **Step 2: Run the test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_version_route.py -q`
Expected: FAIL — `set(body)` is the three existing keys; `AttributeError` for the monkeypatched names.

- [ ] **Step 3: Write the minimal implementation**

In `src/localmail/serve/routes/version.py`, extend the imports:

```python
from localmail import __version__ as SERVER_VERSION
from localmail import __version_source__ as VERSION_SOURCE
from localmail.build_report import resolve_build_info
```

and replace the route:

```python
@router.get("/version")
def version() -> dict[str, object]:
    """Identity of this server: protocol, version, and which build it is.

    `build_source` and `version_source` are always present and never null;
    only `build_hash` is nullable. Without them, "installed from a wheel"
    (normal) and "git ran and failed" (notable) are the same `null` — the shape
    #291 removed from the version line, which #278 would have reintroduced one
    field over.

    The human diagnostic is deliberately absent: this route is unauthenticated
    and `__version_diagnostic__` embeds rendered exception text carrying errno
    values and filesystem paths (#303). Identifiers yes; paths no. The human
    line stays in the server's logs, where #295 put it.
    """
    build = resolve_build_info()
    return {
        "api_major": API_MAJOR,
        "api_minor": API_MINOR,
        "server_version": SERVER_VERSION,
        "build_hash": build.build_hash,
        "build_source": build.source.value,
        "version_source": VERSION_SOURCE.wire_name,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_version_route.py tests/test_serve_app_baseline.py -q`
Expected: PASS. `test_serve_app_baseline.py::test_version_unauth` asserts key-by-key rather than on the whole dict, so added keys do not break it.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/routes/version.py tests/test_serve_version_route.py
git commit -m "feat(serve): report build provenance and version source on /v1/version (#278, #300)"
```

---

### Task 7: #300's CLI half — state the contract, pin it

**Files:**
- Create: `tests/test_cli_version_stderr_contract.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `VersionSource` (Task 5).
- Produces: no code change — a pinned, documented contract.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_version_stderr_contract.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`localmail --version` already has a machine-readable channel (#300).

stderr is non-empty if and only if the version could not be resolved. That has
been true since #291 but was an accident of implementation rather than a stated
contract, and nothing pinned it — so a future refactor could move the line to
stdout, or drop it, without a failing test, and every script checking it would
silently start reporting healthy.

No behaviour changes here. stdout stays the single machine-readable line and
the exit status stays 0, both of which scripts depend on.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from localmail.cli import main
from localmail.version_report import VersionSource, unknown_version_diagnostic

_UNRESOLVABLE = [
    VersionSource.NOT_INSTALLED,
    VersionSource.METADATA_INCOMPLETE,
    VersionSource.METADATA_UNREADABLE,
]


def test_a_healthy_resolution_says_nothing_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", None)
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.strip()


@pytest.mark.parametrize("source", _UNRESOLVABLE, ids=lambda s: s.value)
def test_every_unresolvable_source_writes_to_stderr(
    source: VersionSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail = "boom" if source is VersionSource.METADATA_UNREADABLE else None
    diagnostic = unknown_version_diagnostic(source, detail=detail)
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", diagnostic)

    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0, "exit 0 is contract — scripts use it as liveness"
    assert result.stderr.strip(), f"{source.value} must be reported on stderr"


def test_the_diagnostic_never_reaches_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout is parsed by the manual's install-verification step."""
    diagnostic = unknown_version_diagnostic(
        VersionSource.NOT_INSTALLED, detail=None
    )
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", diagnostic)

    result = CliRunner().invoke(main, ["--version"])

    assert diagnostic not in result.stdout
    assert len(result.stdout.strip().splitlines()) == 1
```

- [ ] **Step 2: Run the test to verify it fails or passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_version_stderr_contract.py -q`
Expected: **PASS**. This task pins existing behaviour rather than changing it, so green here is the correct result — the value is that the contract can no longer be broken silently.

- [ ] **Step 3: Prove the pin is not vacuous by mutation**

Temporarily edit `src/localmail/cli.py`'s `_print_version` to echo the diagnostic to stdout (`err=False`), re-run, and confirm `test_the_diagnostic_never_reaches_stdout` **fails**. Then restore **from a file copy** (`cp` the original aside first) — never `git checkout`, which would discard uncommitted work.

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_version_stderr_contract.py -q`
Expected: FAIL under the mutation, PASS after restoring.

- [ ] **Step 4: Document the contract in README**

In `README.md`, in the version-diagnostic section (the paragraph beginning "**Every command reports it, not just `--version`.**"), append:

```markdown
For scripts, the contract is: **stderr is non-empty if and only if the version
could not be resolved.** stdout stays the single `localmail, version X.Y.Z`
line and the exit status stays `0` in both cases, so neither is a failure
signal — check stderr, or read `version_source` from `/v1/version`, which
reports the same four outcomes as a machine-readable string.
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_cli_version_stderr_contract.py README.md
git commit -m "docs(cli): state and pin the --version stderr contract (#300)"
```

---

### Task 8: The desktop GUI renders it

**Files:**
- Modify: `gui/src-tauri/src/commands/version.rs`
- Modify: `gui/src/lib/api/version.ts`
- Create: `gui/src/lib/build_provenance.ts`
- Create: `gui/src/lib/build_provenance.test.ts`
- Modify: `gui/src/screens/settings/SettingsAbout.svelte`

**Interfaces:**
- Consumes: the wire contract from Task 6.
- Produces: `buildLabel(hash, source)` and `versionWarning(source)` in `build_provenance.ts`.

- [ ] **Step 1: Write the failing TS test**

Create `gui/src/lib/build_provenance.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import { buildLabel, versionWarning } from "./build_provenance";

describe("buildLabel", () => {
  it("shows the hash when there is one", () => {
    expect(buildLabel("eec8e09", "git_checkout")).toBe("eec8e09");
    expect(buildLabel("eec8e09-dirty", "git_checkout")).toBe("eec8e09-dirty");
  });

  it("explains an absent hash rather than showing a bare placeholder", () => {
    expect(buildLabel(null, "not_a_repo")).toBe("— not a repository");
    expect(buildLabel(null, "git_unavailable")).toBe("— git unavailable");
    expect(buildLabel(null, "git_failed")).toBe("— could not read the repository");
  });

  it("falls back to the placeholder for a source it does not know", () => {
    // An older or newer server; the row must not render "undefined".
    expect(buildLabel(null, "something_new")).toBe("?");
    expect(buildLabel(null, null)).toBe("?");
  });
});

describe("versionWarning", () => {
  it("is null for a healthy install", () => {
    expect(versionWarning("installed")).toBeNull();
  });

  it("names the fault for each unresolvable source", () => {
    expect(versionWarning("not_installed")).toBe("not installed");
    expect(versionWarning("metadata_incomplete")).toBe("install damaged");
    expect(versionWarning("metadata_unreadable")).toBe("metadata unreadable");
  });

  it("is null for an unknown or absent source", () => {
    // A server predating the field must not be rendered as broken.
    expect(versionWarning(null)).toBeNull();
    expect(versionWarning("something_new")).toBeNull();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd gui && npx vitest run src/lib/build_provenance.test.ts`
Expected: FAIL — cannot resolve `./build_provenance`.

- [ ] **Step 3: Write the pure module**

Create `gui/src/lib/build_provenance.ts`:

```typescript
/**
 * Human phrasing for the server's build provenance (#278, #300).
 *
 * Pure, and separate from the About tab, per project convention — logic out of
 * components. Both functions accept an unknown source and degrade quietly: the
 * client can be older or newer than the server it is talking to, and a version
 * screen that renders "undefined" or cries wolf about a healthy install is
 * worse than one that says nothing.
 */

const BUILD_REASONS: Record<string, string> = {
  not_a_repo: "— not a repository",
  git_unavailable: "— git unavailable",
  git_failed: "— could not read the repository",
};

const VERSION_FAULTS: Record<string, string> = {
  not_installed: "not installed",
  metadata_incomplete: "install damaged",
  metadata_unreadable: "metadata unreadable",
};

/** The placeholder for "this server told us nothing we understand". */
const UNKNOWN = "?";

/** What the "Server build" row shows. */
export function buildLabel(
  hash: string | null | undefined,
  source: string | null | undefined,
): string {
  if (hash) return hash;
  if (source && source in BUILD_REASONS) return BUILD_REASONS[source];
  return UNKNOWN;
}

/**
 * A short fault phrase to mark the "Server" row with, or null when the
 * server's version is trustworthy. `installed` and anything unrecognised are
 * both null — only a *known* fault is worth alarming about.
 */
export function versionWarning(source: string | null | undefined): string | null {
  if (!source) return null;
  return VERSION_FAULTS[source] ?? null;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd gui && npx vitest run src/lib/build_provenance.test.ts`
Expected: PASS (6 tests).

- [ ] **Step 5: Widen the Rust struct**

In `gui/src-tauri/src/commands/version.rs`:

```rust
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct VersionInfo {
    pub api_major: u32,
    pub api_minor: u32,
    pub server_version: Option<String>,
    pub build_hash: Option<String>,
    // Added #278/#300. `#[serde(default)]` so a server predating these keys
    // still decodes — the same back-compat the `is_admin` field takes.
    #[serde(default)]
    pub build_source: Option<String>,
    #[serde(default)]
    pub version_source: Option<String>,
}
```

There is exactly **one** struct literal to update — `version.rs:86`, inside
`decodes_version_payload` (Rust has no partial struct literal, and this type
derives no `Default`). Add the two fields to it:

```rust
                build_hash: Some("abc123".to_string()),
                build_source: None,
                version_source: None,
```

The two JSON bodies in that module's tests (`version.rs:70` and `:98`) need
**no** change — `#[serde(default)]` is exactly what lets a payload predating
these keys keep decoding, which is the back-compat property worth having a test
for. Confirm with `grep -rn "VersionInfo {" src/` that nothing else appears.

- [ ] **Step 6: Widen the TS type**

In `gui/src/lib/api/version.ts`:

```typescript
export interface ServerVersionInfo extends VersionShape {
  server_version: string | null;
  build_hash: string | null;
  // Optional, not `string | null`: the *server* always sends these, but this
  // client can be talking to one that predates them. That is the same reason
  // `buildLabel`/`versionWarning` accept `null | undefined` — and it is why
  // the ten existing mock sites (MainView, VersionGate, version.test,
  // SettingsAbout) need no change.
  build_source?: string | null;
  version_source?: string | null;
}
```

- [ ] **Step 7: Render it**

In `gui/src/screens/settings/SettingsAbout.svelte`, add to the script block:

```typescript
  import { buildLabel, versionWarning } from "../../lib/build_provenance";
```

and replace the Server / Server build rows in the `<dl>`:

```svelte
    <dt>Server</dt>
    <dd>
      {version.snapshot.info?.server_version ?? "?"}
      {#if versionWarning(version.snapshot.info?.version_source)}
        <span class="fault">({versionWarning(version.snapshot.info?.version_source)})</span>
      {/if}
    </dd>
    <dt>Server build</dt>
    <dd>{buildLabel(version.snapshot.info?.build_hash, version.snapshot.info?.build_source)}</dd>
```

and add to the `<style>` block:

```css
  .fault {
    color: var(--color-danger, #b3261e);
  }
```

- [ ] **Step 8: Write the component tests**

The pure module proves the mapping; these prove the About tab actually *calls*
it. Without them, `buildLabel` could be correct and unwired and every test would
stay green — which is #278's own defect (a field declared everywhere and fed by
nothing).

Append to the existing `gui/src/screens/settings/SettingsAbout.test.ts`, inside
its `describe("SettingsAbout", ...)` block:

```typescript
  it("explains an absent build rather than showing a bare placeholder", () => {
    Object.assign(version.snapshot, {
      info: {
        api_major: 1,
        api_minor: 0,
        server_version: "0.3.0",
        build_hash: null,
        build_source: "not_a_repo",
        version_source: "installed",
      },
      compatible: true,
    });

    const { getByText } = render(SettingsAbout);

    expect(getByText("— not a repository")).toBeTruthy();
  });

  it("marks the server row when its version could not be resolved", () => {
    Object.assign(version.snapshot, {
      info: {
        api_major: 1,
        api_minor: 0,
        server_version: "0.0.0+unknown",
        build_hash: "eec8e09",
        build_source: "git_checkout",
        version_source: "metadata_unreadable",
      },
      compatible: true,
    });

    const { getByText } = render(SettingsAbout);

    // #300: the sentinel used to render as though it were a version.
    expect(getByText("(metadata unreadable)")).toBeTruthy();
  });

  it("shows no marker for a healthy install", () => {
    Object.assign(version.snapshot, {
      info: {
        api_major: 1,
        api_minor: 0,
        server_version: "0.3.0",
        build_hash: "eec8e09",
        build_source: "git_checkout",
        version_source: "installed",
      },
      compatible: true,
    });

    const { queryByText } = render(SettingsAbout);

    // The positive control for the two above: a rule that always marked the row
    // would satisfy them both and cry wolf on every healthy install.
    expect(queryByText(/\(.*unreadable.*\)/)).toBeNull();
    expect(queryByText("eec8e09")).toBeTruthy();
  });
```

Run: `cd gui && npx vitest run src/screens/settings/SettingsAbout.test.ts`
Expected: PASS.

- [ ] **Step 9: Run the full GUI gates**

Run:
```bash
cd gui && npm run check && npm test && npm run build
cd src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings
```
Expected: 0 svelte-check errors; all vitest and cargo tests pass. Fix any
`VersionInfo` literal the compiler names as missing fields.

- [ ] **Step 10: Commit**

```bash
git add gui/
git commit -m "feat(gui): render the server build, and explain an absent one (#278, #300)"
```

---

### Task 9: Documentation and the full-suite gate

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Add the module to CLAUDE.md's layout tree**

In the `src/localmail/` block, after the `version_report.py` lines:

```
  build_report.py   # pure enum + BuildInfo, lazy git resolution (#278, #300)
```

- [ ] **Step 2: Add the architectural note to CLAUDE.md**

After the version-diagnostic section (the `__init__.py` exports bullet), add:

```markdown
- **The build identity is resolved from the checkout, not stamped at build time
  (#278, #300).** `/v1/version` now carries `build_hash`, `build_source` and
  `version_source`. Design:
  [docs/superpowers/specs/2026-08-15-build-provenance-design.md](docs/superpowers/specs/2026-08-15-build-provenance-design.md).
  - **There is no build**, which is why. Both CI workflows are test-only, there
    are no tags, nothing publishes, and *both* deployments run editable installs
    from a git checkout — so a hash stamped into a wheel would be absent on the
    only machines the row is ever read on. `STAMPED` is a declared seam reading
    a `_build_info.py` nothing writes; implement the hatchling hook when a
    release pipeline exists, not before.
  - **`build_report.py` never logs and no source carries a remedy** — the one
    place it deliberately breaks from `version_report`. An unresolvable
    *version* is always a fault; an unresolvable *build hash* usually is not,
    since `NOT_A_REPO` is the correct state of an installed artifact. Copying
    `VersionSource`'s forced-remedy rule across would put an ERROR in front of
    an operator for a healthy install, i.e. #291 inverted.
  - **Resolution is lazy and cached, never at import.** `import localmail` runs
    for all 38 CLI commands and a `git` subprocess there can hang on a stale
    mount — the #296 scenario. Caching also gives the semantics the row wants:
    pinned for the life of the process, so it reports what the process is
    *running*, not what the tree says now. That is live on an editable install,
    where a `git pull` moves the tree under a daemon that keeps executing the
    code it already imported. `reset_build_info()` + an autouse conftest
    fixture, the `reset_version_reports()` shape.
  - **The repo it finds must be ours**, checked by requiring
    `<toplevel>/src/localmail/__init__.py` to resolve to the file we imported.
    Containment is not enough: a virtualenv inside a dotfiles repo *is*
    contained, and would report that project's SHA as localmail's build. Its
    own test, because it fails silently.
  - **`-dirty` measures tracked files only** (`git diff --quiet HEAD`). Scratch
    files would make every deployment read dirty forever, and a marker that is
    always on carries no information. A single
    `git describe --always --dirty` would halve the subprocess count and was
    **rejected**: the day someone tags a release it silently returns
    `v0.4.0-3-geec8e09-dirty`, changing the field's format under us.
  - **The wire strings are declared, never derived.** `BuildSource`'s value IS
    its wire string; `VersionSource` carries a separate `wire_name`, because its
    own values are hyphenated debugging aids (`"not-installed"`) while this
    API's wire enums are underscored (`rewrite_note_code` ships
    `not_configured`). `reject_empty_wire_name` enforces non-emptiness at class
    creation, beside `reject_empty_diagnostic` and for the same reason.
  - **The diagnostic text is deliberately NOT on the wire.** `/v1/version` is
    unauthenticated and `__version_diagnostic__` embeds rendered exception text
    carrying errno values and filesystem paths (#303). `version_source` is an
    identifier; the human line stays in the logs where #295 put it. If a
    machine-readable *reason* beyond the enum is ever wanted, it belongs on an
    authenticated endpoint.
  - **`--version` gained no flag.** stderr is non-empty iff the version is
    unresolvable — true since #291, now stated in README and pinned by
    `tests/test_cli_version_stderr_contract.py`. stdout stays the single line
    and exit stays 0, so neither is a failure signal.
```

- [ ] **Step 3: Document the wire in README**

In `README.md`, near the `/v1/version` description, add:

```markdown
`GET /v1/version` (unauthenticated) reports six fields: `api_major`,
`api_minor`, `server_version`, `build_hash`, `build_source`, `version_source`.

`build_hash` is the short git SHA of the checkout the server is running,
suffixed `-dirty` when tracked files differ from it — the answer to "did the
daemon get restarted after my pull?". It is `null` when there is no identity to
report, and `build_source` says why: `git_checkout`, `stamped`, `not_a_repo`,
`git_unavailable`, `git_failed`.

`version_source` is `installed` on a healthy install, and `not_installed`,
`metadata_incomplete` or `metadata_unreadable` when `server_version` is the
`0.0.0+unknown` sentinel — so a monitoring client can alert on a broken install
rather than displaying the sentinel as though it were a version. Both source
fields are always present; only `build_hash` is nullable.
```

- [ ] **Step 4: Run every gate**

Run:
```bash
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
unset VIRTUAL_ENV && uv run mypy src/localmail
ruff check src/localmail/build_report.py src/localmail/version_report.py \
  src/localmail/serve/routes/version.py
cd gui && npm run check && npm test && npm run build && cd ..
```

Expected: pytest green with **no skips** beyond the known platform one; mypy
`Success`; ruff clean on the changed files (repo-wide stays at its 10
pre-existing errors, #285); svelte-check 0 errors.

**Measure the test-count baseline on both refs in this session** — do not quote
a number from a handoff:

```bash
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2
```

- [ ] **Step 5: Verify against the live archive**

```bash
unset VIRTUAL_ENV && uv run python -c "
from localmail.build_report import resolve_build_info
print(resolve_build_info())
"
```
Expected: `BuildInfo(build_hash='<short sha>', source=<BuildSource.GIT_CHECKOUT>)`,
matching `git rev-parse --short HEAD`.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: record the build-provenance rules (#278, #300)"
```

---

## Done when

- `/v1/version` returns six keys; `build_source` and `version_source` are never null.
- The About tab shows a real SHA on both deployments, and explains an absent one.
- #278 and #300 can both be closed.
- Full pytest suite, mypy, ruff-on-changed-files, svelte-check, vitest, cargo test and both clippy invocations are green.
- One branch, one PR, based on `main`.
