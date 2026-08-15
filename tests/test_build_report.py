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
    _GIT_TIMEOUT_S, _STRIPPED_GIT_ENV, UNIDENTIFIED_SOURCES, BuildInfo,
    BuildSource, _resolve_from_package_dir, reject_empty_wire_value,
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
@pytest.mark.parametrize("hash_value", ["eec8e09", "", "   "])
def test_an_unidentified_source_may_not_carry_a_hash(
    source: BuildSource, hash_value: str
) -> None:
    with pytest.raises(ValueError, match="build_hash"):
        BuildInfo(build_hash=hash_value, source=source)


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
    """A stray GIT_DIR makes `-C` a no-op, pointing us at another repo.

    GIT_INDEX_FILE is in the set for a narrower reason than the rest: it does
    not move the repository, it swaps the index the `-dirty` verdict is read
    against.
    """
    seen: dict[str, str] = {}

    def capture(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, returncode=128, stdout="", stderr="")

    for name in _STRIPPED_GIT_ENV:
        monkeypatch.setenv(name, "/somewhere/else")
    # Positive control: only the discovery/index variables are stripped, not
    # every GIT_* an operator's shell happens to export.
    monkeypatch.setenv("GIT_EDITOR", "vim")
    monkeypatch.setattr("localmail.build_report.subprocess.run", capture)

    _resolve_from_package_dir(tmp_path)

    assert [k for k in _STRIPPED_GIT_ENV if k in seen] == []
    assert seen["GIT_EDITOR"] == "vim"


def test_every_git_call_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler for a timeout is tested; that one is ever *requested* was not.

    Deleting `timeout=` entirely left the suite green, because the timeout test
    injects `TimeoutExpired` from a mock rather than provoking one. This is what
    stops a wedged mount holding the first `/v1/version` open.
    """
    timeouts: list[float | None] = []

    def capture(argv, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, returncode=128, stdout="", stderr="")

    monkeypatch.setattr("localmail.build_report.subprocess.run", capture)

    _resolve_from_package_dir(tmp_path)

    assert timeouts == [_GIT_TIMEOUT_S]


def _probe_returning(returncode: int, stdout: str = "", stderr: str = ""):
    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, returncode=returncode, stdout=stdout, stderr=stderr
        )
    return run


def test_exit_128_is_the_only_non_zero_that_reads_as_an_installed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """128 is git's "not a usable repository" — the whole of NOT_A_REPO."""
    monkeypatch.setattr(
        "localmail.build_report.subprocess.run",
        _probe_returning(128, stderr="fatal: not a git repository"),
    )

    info = _resolve_from_package_dir(tmp_path)

    assert info.source is BuildSource.NOT_A_REPO
    assert info.build_hash is None


@pytest.mark.parametrize("returncode", [-9, -11, 1, 129])
def test_any_other_non_zero_exit_is_a_failure_not_a_verdict(
    returncode: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal kill (OOM reports -9) is not "an installed artifact".

    `!= 0 -> NOT_A_REPO` reported a broken host as the healthy state, which is
    the collapse this module exists to end, one probe in. The dirty probe below
    already applied the stricter rule; the two disagreed.
    """
    monkeypatch.setattr(
        "localmail.build_report.subprocess.run",
        _probe_returning(returncode, stderr="boom"),
    )

    info = _resolve_from_package_dir(tmp_path)

    assert info.source is BuildSource.GIT_FAILED
    assert info.build_hash is None


def test_a_probe_answering_in_an_unparseable_shape_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One line where two were asked for.

    Unpinned, deleting the guard let the unpack below raise `IndexError` out of
    a function whose contract is that it never raises — a 500 on an
    unauthenticated route.
    """
    monkeypatch.setattr(
        "localmail.build_report.subprocess.run",
        _probe_returning(0, stdout="/only/one/line\n"),
    )

    info = _resolve_from_package_dir(tmp_path)

    assert info.source is BuildSource.GIT_FAILED
    assert info.build_hash is None


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


def _ours(tmp_path: Path) -> Path:
    """A layout `_repo_is_ours` accepts, so the dirty probe is actually reached."""
    package_dir = tmp_path / "src" / "localmail"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("")
    return package_dir


def _first_probe_ok_then(tmp_path: Path, second):
    def run(argv, **kwargs):
        if "diff" in argv:
            return second(argv, **kwargs)
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=f"{tmp_path}\neec8e09\n", stderr=""
        )
    return run


@pytest.mark.parametrize("returncode", [-9, 2, 128, 129])
def test_a_non_verdict_exit_from_the_dirty_probe_is_named(
    returncode: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 = clean, 1 = dirty. Anything else is git failing, not a verdict.

    Unpinned, deleting that guard reported `eec8e09` / `git_checkout` — an
    affirmative *clean tree* — for a probe that never ran successfully. The
    `-dirty` marker silently absent is worse than a named failure, because it
    is the marker an operator reads to know a deployment was edited.
    """
    monkeypatch.setattr(
        "localmail.build_report.subprocess.run",
        _first_probe_ok_then(
            tmp_path, _probe_returning(returncode, stderr="fatal: bad object")
        ),
    )

    info = _resolve_from_package_dir(_ours(tmp_path))

    assert info.source is BuildSource.GIT_FAILED
    assert info.build_hash is None


def test_a_missing_git_on_the_dirty_probe_reads_the_same_as_on_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Practically unreachable; pinned so the two probes cannot disagree."""
    def gone(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(
        "localmail.build_report.subprocess.run",
        _first_probe_ok_then(tmp_path, gone),
    )

    info = _resolve_from_package_dir(_ours(tmp_path))

    assert info.source is BuildSource.GIT_UNAVAILABLE


def test_an_unreadable_package_tree_does_not_escape_the_identity_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Path.exists()` re-raises most `OSError`s, and it sat outside the `try`.

    Raised at the seam rather than provoked with a `chmod`, for the reason
    `parser`'s #314 guard is tested that way: what is reachable through a real
    fixture depends on the interpreter's own errno list, so a fixture-driven
    test would quietly stop exercising this on the versions that need it.
    """
    monkeypatch.setattr(
        "localmail.build_report.subprocess.run",
        _probe_returning(0, stdout=f"{tmp_path}\neec8e09\n"),
    )
    monkeypatch.setattr(
        Path, "exists",
        lambda self: (_ for _ in ()).throw(PermissionError(13, "Permission denied")),
    )

    info = _resolve_from_package_dir(_ours(tmp_path))

    assert info.source is BuildSource.NOT_A_REPO
    assert info.build_hash is None


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_wire_value_is_rejected_at_class_creation(value: str) -> None:
    """The value IS the wire string here, so an empty one is an empty answer.

    Module-level, and tested by direct call, for `reject_empty_wire_name`'s
    reason: enum machinery replaces `__new__` after class creation, so no test
    can reach the production one to prove the rule fires for a future member.
    """
    with pytest.raises(ValueError, match="empty wire value"):
        reject_empty_wire_value(value)


def test_the_wire_value_guard_is_wired_into_the_constructor() -> None:
    """A guard proven by direct call is worthless if nothing calls it.

    Structural for the same reason as its `VersionSource` sibling: the
    production `__new__` is unreachable once the class exists, and the prose
    around it names the function.
    """
    import ast

    import localmail.build_report as br

    tree = ast.parse(Path(br.__file__).read_text())
    (cls,) = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "BuildSource"
    ]
    (ctor,) = [
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "__new__"
    ]
    called = {
        n.func.id for n in ast.walk(ctor)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }

    assert "reject_empty_wire_value" in called


def test_every_source_declares_whether_it_carries_a_hash() -> None:
    """`identifies` rides the member, so omitting it is a TypeError at import.

    The enforcement is `__new__`'s arity and cannot be reached from a test —
    a member written `NEW = "new"` fails `import localmail.build_report`
    outright. This asserts the payload is present on every member, which is
    what makes the derived `UNIDENTIFIED_SOURCES` trustworthy.
    """
    assert all(isinstance(s.identifies, bool) for s in BuildSource)
    assert UNIDENTIFIED_SOURCES == frozenset(
        s for s in BuildSource if not s.identifies
    )


def test_the_wire_name_of_a_build_source_is_its_value() -> None:
    """An alias, so every wire enum answers the same question by one name.

    `VersionSource` needs a separate `wire_name` because its values are
    debugging aids; here the value is the contract. The route reads
    `.wire_name` on both, so nobody has to know which is which to read it.
    """
    assert all(s.wire_name == s.value for s in BuildSource)


def test_a_git_failure_says_what_git_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The one source that logs, because it is the one that is always a fault.

    `capture_output=True` means git's own account of the failure is already in
    hand; discarding it is the silent catch CLAUDE.md forbids of the sibling
    module's identical broad catch.
    """
    monkeypatch.setattr(
        "localmail.build_report.subprocess.run",
        _probe_returning(129, stderr="usage: git rev-parse"),
    )

    with caplog.at_level("WARNING", logger="localmail.build_report"):
        info = _resolve_from_package_dir(tmp_path)

    assert info.source is BuildSource.GIT_FAILED
    assert "usage: git rev-parse" in caplog.text
    assert "129" in caplog.text


def test_a_raised_failure_logs_the_whole_cause_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Reuses `version_report.render_exception_chain` — one rendering rule.

    A wrapper is the normal shape here (a `sys.meta_path` finder, a subprocess
    helper), and the errno and filename are on the *inner* exception.
    """
    def wrapped(*_args, **_kwargs):
        raise RuntimeError("probe failed") from OSError(5, "I/O error", "/nfs/git")

    monkeypatch.setattr("localmail.build_report.subprocess.run", wrapped)

    with caplog.at_level("WARNING", logger="localmail.build_report"):
        info = _resolve_from_package_dir(tmp_path)

    assert info.source is BuildSource.GIT_FAILED
    assert "probe failed" in caplog.text
    assert "/nfs/git" in caplog.text


@pytest.mark.parametrize(
    "returncode, expected",
    [(128, BuildSource.NOT_A_REPO)],
)
def test_a_healthy_resolution_says_nothing(
    returncode: int, expected: BuildSource,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The positive control, and the reason the silence is scoped not absolute.

    `not_a_repo` is the correct state of an installed artifact. Reporting it
    would put a warning in front of an operator for a healthy install — #291
    inverted, which is exactly what this module refuses to do.
    """
    monkeypatch.setattr(
        "localmail.build_report.subprocess.run", _probe_returning(returncode)
    )

    with caplog.at_level("WARNING", logger="localmail.build_report"):
        info = _resolve_from_package_dir(tmp_path)

    assert info.source is expected
    assert caplog.text == ""


@requires_git
def test_a_real_clean_checkout_says_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half of the control: the success path is silent too."""
    package_dir = _make_repo(tmp_path)

    with caplog.at_level("WARNING", logger="localmail.build_report"):
        info = _resolve_from_package_dir(package_dir)

    assert info.source is BuildSource.GIT_CHECKOUT
    assert caplog.text == ""


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
