# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Build provenance: the value, and why it is what it is (#278).

`build_hash` is worthless without a way to say why it is absent — "installed
from a wheel" (normal) and "git ran and failed" (notable) are the same `null`
otherwise, which is the shape #291 spent four sessions removing from the
version line.
"""
from __future__ import annotations

import dataclasses

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
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.build_hash = "other"  # type: ignore[misc]


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
