# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The file-backed secret store — the backend that lets a headless daemon read
its credentials at boot, with no interactive session to unlock anything."""
from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from localmail.secrets_file import (
    FileSecretStore,
    InsecureSecretsDirectory,
    InsecureSecretsFile,
    StaleSecretsTempFile,
)
from localmail.secrets_store import (
    SECRETS_DIR_MODE,
    SECRETS_FILE_MODE,
    SecretsFileCorrupt,
)


@pytest.fixture
def store(tmp_path: Path) -> FileSecretStore:
    return FileSecretStore(tmp_path / "nested" / "secrets.json")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_set_then_get_round_trips(store: FileSecretStore) -> None:
    store.set("gmail", "hunter2")
    assert store.get("gmail") == "hunter2"


def test_get_of_an_unset_key_is_none(store: FileSecretStore) -> None:
    store.set("gmail", "hunter2")
    assert store.get("other") is None


def test_missing_file_reads_as_no_secrets(store: FileSecretStore) -> None:
    """A fresh install has no file yet; `list-accounts` must still run."""
    assert store.get("gmail") is None


def test_set_overwrites_an_existing_value(store: FileSecretStore) -> None:
    store.set("gmail", "old")
    store.set("gmail", "new")
    assert store.get("gmail") == "new"


def test_set_preserves_the_other_entries(store: FileSecretStore) -> None:
    store.set("a", "1")
    store.set("b", "2")
    store.set("a", "3")
    assert store.get("b") == "2"


def test_delete_removes_only_its_own_key(store: FileSecretStore) -> None:
    store.set("a", "1")
    store.set("b", "2")
    store.delete("a")
    assert store.get("a") is None
    assert store.get("b") == "2"


def test_delete_of_an_absent_key_is_a_noop(store: FileSecretStore) -> None:
    """Matches the keyring backend, which swallows PasswordDeleteError."""
    store.delete("never-set")


def test_delete_on_a_missing_file_is_a_noop(store: FileSecretStore) -> None:
    store.delete("anything")
    assert not store.path.exists()


def test_created_file_is_owner_only(store: FileSecretStore) -> None:
    store.set("gmail", "hunter2")
    assert _mode(store.path) == SECRETS_FILE_MODE


def test_created_directory_is_owner_only(store: FileSecretStore) -> None:
    store.set("gmail", "hunter2")
    assert _mode(store.path.parent) == SECRETS_DIR_MODE


def test_rewrite_keeps_the_file_owner_only(store: FileSecretStore) -> None:
    """The atomic replace must not inherit the umask on the second write."""
    store.set("gmail", "hunter2")
    store.set("gmail", "hunter3")
    assert _mode(store.path) == SECRETS_FILE_MODE


def test_no_temp_file_is_left_behind(store: FileSecretStore) -> None:
    store.set("gmail", "hunter2")
    leftovers = [p.name for p in store.path.parent.iterdir() if p != store.path]
    assert leftovers == []


def test_a_group_readable_file_is_refused(store: FileSecretStore) -> None:
    """Refusing loudly beats reading a leaked secrets file for weeks. The error
    has to name the file and the fix, since the daemon will not start until the
    operator acts on it."""
    store.set("gmail", "hunter2")
    store.path.chmod(0o640)
    with pytest.raises(InsecureSecretsFile) as exc:
        store.get("gmail")
    assert str(store.path) in str(exc.value)
    assert "chmod 600" in str(exc.value)


def test_a_world_readable_file_is_refused(store: FileSecretStore) -> None:
    store.set("gmail", "hunter2")
    store.path.chmod(0o644)
    with pytest.raises(InsecureSecretsFile):
        store.get("gmail")


def test_writing_to_an_insecure_file_is_also_refused(store: FileSecretStore) -> None:
    """`set` reads the existing entries first, so it must not be a way to
    sidestep the check — and silently rewriting the file at 0600 would hide
    that the secrets had been exposed."""
    store.set("gmail", "hunter2")
    store.path.chmod(0o644)
    with pytest.raises(InsecureSecretsFile):
        store.set("other", "x")


def test_a_world_writable_parent_directory_is_refused(store: FileSecretStore) -> None:
    """The file's own 0600 is no protection against a writable parent: anyone
    with that write bit can rename the file away and drop in their own 0600
    substitute, which passes every check the file-mode rule can make. A
    world-writable config directory is never legitimate, so this refuses (#246).
    """
    store.set("gmail", "hunter2")
    store.path.parent.chmod(0o777)
    with pytest.raises(InsecureSecretsDirectory) as exc:
        store.get("gmail")
    assert str(store.path.parent) in str(exc.value)
    assert "chmod o-w" in str(exc.value)


def test_writing_into_a_world_writable_parent_is_refused_too(
    store: FileSecretStore,
) -> None:
    """`set` reads first, so it cannot be a way around the check — and storing a
    fresh secret into a directory anyone can substitute is the worst moment to
    stay quiet."""
    store.set("gmail", "hunter2")
    store.path.parent.chmod(0o777)
    with pytest.raises(InsecureSecretsDirectory):
        store.set("other", "x")


def test_a_world_writable_parent_is_refused_before_any_file_exists(
    tmp_path: Path,
) -> None:
    """The substitution works just as well by *planting* a file where none was.
    Refusing at the first touch beats letting the operator store a secret there
    and only complaining on the next read."""
    parent = tmp_path / "cfg"
    parent.mkdir()
    parent.chmod(0o777)  # not mkdir(mode=…) — that is masked by the umask
    store = FileSecretStore(parent / "secrets.json")
    with pytest.raises(InsecureSecretsDirectory):
        store.get("gmail")


def test_a_group_writable_parent_directory_warns_but_still_works(
    store: FileSecretStore, caplog: pytest.LogCaptureFixture
) -> None:
    """0775 is what a stock umask-002 + private-group distro gives a directory
    the user made, where the group is that user alone. Refusing would wedge a
    safe install over a distro default, so this warns and carries on."""
    store.set("gmail", "hunter2")
    store.path.parent.chmod(0o775)
    with caplog.at_level(logging.WARNING, logger="localmail.secrets_file"):
        assert store.get("gmail") == "hunter2"
    assert any(
        str(store.path.parent) in r.message and "chmod g-w" in r.message
        for r in caplog.records
    ), caplog.text


def test_the_group_writable_warning_is_logged_once_per_store(
    store: FileSecretStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Every `get` re-reads the file, and the daemon reads a secret on each
    reconnect — an un-deduplicated warning would bury the log it shares with the
    sync errors an operator actually needs to see."""
    store.set("gmail", "hunter2")
    store.path.parent.chmod(0o775)
    with caplog.at_level(logging.WARNING, logger="localmail.secrets_file"):
        for _ in range(3):
            store.get("gmail")
    assert len(caplog.records) == 1, caplog.text


def test_a_traversable_parent_directory_is_accepted_in_silence(
    store: FileSecretStore, caplog: pytest.LogCaptureFixture
) -> None:
    """`~/.config` is routinely 0755. Read and execute bits let somebody learn
    the file's *name*; they grant no rename and no read of its contents."""
    store.set("gmail", "hunter2")
    store.path.parent.chmod(0o755)
    with caplog.at_level(logging.WARNING, logger="localmail.secrets_file"):
        assert store.get("gmail") == "hunter2"
    assert caplog.records == []


def test_a_missing_parent_directory_is_not_an_error(store: FileSecretStore) -> None:
    """A fresh install: `_write` will create it at SECRETS_DIR_MODE, so there is
    nothing yet to grade."""
    assert not store.path.parent.exists()
    assert store.get("gmail") is None


def test_corrupt_file_raises_rather_than_reading_as_empty(
    store: FileSecretStore,
) -> None:
    store.set("gmail", "hunter2")
    store.path.write_text("{ this is not json", encoding="utf-8")
    store.path.chmod(SECRETS_FILE_MODE)
    with pytest.raises(SecretsFileCorrupt):
        store.get("gmail")


def test_values_survive_a_reopen(tmp_path: Path) -> None:
    """The whole point: a different process — the daemon after a reboot — reads
    what the CLI wrote, with nothing unlocked in between."""
    path = tmp_path / "secrets.json"
    FileSecretStore(path).set("gmail:refresh", "1//0aBc-_")
    assert FileSecretStore(path).get("gmail:refresh") == "1//0aBc-_"


def test_non_ascii_secret_round_trips(store: FileSecretStore) -> None:
    store.set("acct", "pä§§wörd✓")
    assert store.get("acct") == "pä§§wörd✓"


def _stray_temp(store: FileSecretStore) -> Path:
    """The temp path `_write` uses, as an interrupted write would have left it."""
    return store.path.with_name(f".{store.path.name}.tmp")


def test_a_stale_temp_file_raises_an_actionable_error(store: FileSecretStore) -> None:
    """O_EXCL on a fixed temp name means one interrupted write wedges every
    later one. A SIGKILL leaves no handler to clean up, so the message has to
    carry the recovery — a bare FileExistsError naming a dotfile does not."""
    store.set("gmail", "hunter2")
    _stray_temp(store).write_text("interrupted")
    with pytest.raises(StaleSecretsTempFile) as exc:
        store.set("gmail", "hunter3")
    assert str(_stray_temp(store)) in str(exc.value)
    assert "rm " in str(exc.value)


def test_a_stale_temp_file_is_never_clobbered(store: FileSecretStore) -> None:
    """It may hold the secret from the interrupted write, so refusing has to
    mean leaving it intact for the operator to inspect."""
    store.set("gmail", "hunter2")
    _stray_temp(store).write_text("possibly-a-secret")
    with pytest.raises(StaleSecretsTempFile):
        store.set("gmail", "hunter3")
    assert _stray_temp(store).read_text() == "possibly-a-secret"
    assert store.get("gmail") == "hunter2", "the live file must be untouched"


def test_writes_resume_once_the_stale_temp_is_removed(store: FileSecretStore) -> None:
    store.set("gmail", "hunter2")
    _stray_temp(store).write_text("interrupted")
    with pytest.raises(StaleSecretsTempFile):
        store.set("gmail", "hunter3")
    _stray_temp(store).unlink()
    store.set("gmail", "hunter3")
    assert store.get("gmail") == "hunter3"


def test_a_failed_write_leaves_no_temp_behind(store: FileSecretStore) -> None:
    """The cleanup handler is what keeps the stale-temp path rare — without it
    every serialisation failure would wedge the store until an operator noticed.
    """
    store.set("gmail", "hunter2")
    # json.dumps rejects it, and it does so *after* the temp has been created.
    with pytest.raises(TypeError):
        store.set("bad", object())  # type: ignore[arg-type]
    assert not _stray_temp(store).exists()
    assert store.get("gmail") == "hunter2"
