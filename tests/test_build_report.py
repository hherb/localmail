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
import shutil
import subprocess
from pathlib import Path

import pytest

from localmail.build_report import (
    UNIDENTIFIED_SOURCES, BuildInfo, BuildSource, _resolve_from_package_dir,
    reset_build_info, resolve_build_info,
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


requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not installed"
)


def _git(repo: Path, *args: str) -> None:
    """Run git against the fixture repo, isolated from the developer's config.

    `commit.gpgsign = true` in a global config makes the fixture commit below
    block on a signing key, so these tests would pass or hang depending on
    whose machine they run on.
    """
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
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
def test_a_repo_path_containing_a_space_still_resolves(tmp_path: Path) -> None:
    """`.split()` would yield 3+ tokens here and report GIT_FAILED."""
    spaced = tmp_path / "a directory with spaces"
    spaced.mkdir()
    package_dir = _make_repo(spaced)

    info = _resolve_from_package_dir(package_dir)

    assert info.source is BuildSource.GIT_CHECKOUT
    assert info.build_hash is not None


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


def test_a_failure_on_the_dirty_probe_is_named_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second git call has its own guard, and it must actually catch.

    Every other injected-failure test fails at the FIRST call and returns
    before the dirty probe is reached, so this branch shipped unverified.
    Needs no real git: the layout satisfies `_repo_is_ours`, and the probe
    is what raises.
    """
    package_dir = tmp_path / "src" / "localmail"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("")

    def ok_then_boom(argv, **_kwargs):
        if "diff" in argv:
            raise OSError("git died mid-probe")
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=f"{tmp_path}\neec8e09\n", stderr=""
        )

    monkeypatch.setattr("localmail.build_report.subprocess.run", ok_then_boom)

    info = _resolve_from_package_dir(package_dir)

    assert info.source is BuildSource.GIT_FAILED
    assert info.build_hash is None


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
