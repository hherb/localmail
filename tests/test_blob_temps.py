# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Orphaned attachment-blob temp files: naming, expiry, and the sweep (#237).

#231 gave every attachment writer a private temp name so two concurrent
writers of the same blob could not truncate each other's temp. That removed an
accidental self-limiting property of the old shared `<sha>.tmp`: a hard kill
(SIGKILL / OOM / power loss) between `write_bytes` and `replace` now strands a
name nothing ever reuses. These tests pin the collector.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from localmail.blob_temps import (
    SweepResult,
    is_expired,
    is_writer_temp,
    new_temp_path,
    sweep_blob_temps,
    temp_name,
)

SHA = "a" * 64
OTHER_SHA = "b" * 64


# --- naming (pure) ---------------------------------------------------------


def test_temp_name_is_recognised_by_the_sweeper() -> None:
    """The minter and the matcher must never drift — one module owns both."""
    name = temp_name(SHA, pid=4242, token=uuid.uuid4().hex)
    assert is_writer_temp(name)


def test_new_temp_path_sits_beside_its_blob_and_is_recognised() -> None:
    blob = Path("/var/localmail/blobs/aa/bb") / SHA
    tmp = new_temp_path(blob)
    assert tmp.parent == blob.parent
    assert is_writer_temp(tmp.name)
    assert str(os.getpid()) in tmp.name


def test_two_temp_paths_for_one_blob_never_collide() -> None:
    blob = Path("/var/localmail/blobs/aa/bb") / SHA
    assert new_temp_path(blob) != new_temp_path(blob)


def test_canonical_blob_name_is_not_a_temp() -> None:
    assert not is_writer_temp(SHA)


def test_unrelated_dot_tmp_files_are_not_writer_temps() -> None:
    """Strict shape matching: never collect a file localmail did not mint."""
    assert not is_writer_temp("notes.tmp")
    assert not is_writer_temp(f"{SHA}.tmp")  # the pre-#231 shared name
    assert not is_writer_temp(f"{SHA}.1234.tmp")  # no uuid token
    assert not is_writer_temp(f"{SHA}.abc.{uuid.uuid4().hex}.tmp")  # pid not numeric
    assert not is_writer_temp(f"{'z' * 64}.1234.{uuid.uuid4().hex}.tmp")  # not hex


# --- expiry (pure) ---------------------------------------------------------


def test_expiry_is_strictly_older_than_the_gate() -> None:
    assert is_expired(mtime=100.0, now=200.0, max_age_s=99.0)
    assert not is_expired(mtime=100.0, now=200.0, max_age_s=100.0)
    assert not is_expired(mtime=100.0, now=200.0, max_age_s=101.0)


def test_a_future_mtime_is_never_expired() -> None:
    """Clock skew or a touched file must not make a live write collectable."""
    assert not is_expired(mtime=500.0, now=200.0, max_age_s=1.0)


# --- sweep (IO) ------------------------------------------------------------


def _blob_dir(root: Path, sha: str) -> Path:
    d = root / "blobs" / sha[:2] / sha[2:4]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _age(path: Path, seconds: float, *, now: float) -> None:
    os.utime(path, (now - seconds, now - seconds))


def test_sweep_removes_expired_temps_and_reports_bytes(tmp_path: Path) -> None:
    now = 1_000_000.0
    d = _blob_dir(tmp_path, SHA)
    old = d / temp_name(SHA, pid=1, token=uuid.uuid4().hex)
    old.write_bytes(b"x" * 7)
    _age(old, 90_000, now=now)

    result = sweep_blob_temps(tmp_path, max_age_s=86_400, now=now)

    assert not old.exists()
    assert result == SweepResult(scanned=1, removed=1, bytes_reclaimed=7, errors=0)


def test_sweep_leaves_a_temp_younger_than_the_gate(tmp_path: Path) -> None:
    """Age-gating is what keeps an in-flight writer's temp safe."""
    now = 1_000_000.0
    d = _blob_dir(tmp_path, SHA)
    fresh = d / temp_name(SHA, pid=os.getpid(), token=uuid.uuid4().hex)
    fresh.write_bytes(b"y" * 3)
    _age(fresh, 10, now=now)

    result = sweep_blob_temps(tmp_path, max_age_s=86_400, now=now)

    assert fresh.exists()
    assert result == SweepResult(scanned=1, removed=0, bytes_reclaimed=0, errors=0)


def test_sweep_never_touches_a_canonical_blob(tmp_path: Path) -> None:
    now = 1_000_000.0
    d = _blob_dir(tmp_path, SHA)
    blob = d / SHA
    blob.write_bytes(b"payload")
    _age(blob, 10_000_000, now=now)

    result = sweep_blob_temps(tmp_path, max_age_s=1, now=now)

    assert blob.read_bytes() == b"payload"
    assert result.scanned == 0


def test_dry_run_reports_what_it_would_reclaim_without_deleting(tmp_path: Path) -> None:
    now = 1_000_000.0
    d = _blob_dir(tmp_path, SHA)
    old = d / temp_name(SHA, pid=1, token=uuid.uuid4().hex)
    old.write_bytes(b"z" * 11)
    _age(old, 90_000, now=now)

    result = sweep_blob_temps(tmp_path, max_age_s=86_400, now=now, dry_run=True)

    assert old.exists()
    assert result == SweepResult(scanned=1, removed=1, bytes_reclaimed=11, errors=0)


def test_sweep_walks_the_whole_fan_out(tmp_path: Path) -> None:
    now = 1_000_000.0
    for sha in (SHA, OTHER_SHA):
        p = _blob_dir(tmp_path, sha) / temp_name(sha, pid=1, token=uuid.uuid4().hex)
        p.write_bytes(b"q" * 5)
        _age(p, 90_000, now=now)

    result = sweep_blob_temps(tmp_path, max_age_s=86_400, now=now)

    assert result.removed == 2
    assert result.bytes_reclaimed == 10


def test_missing_blob_tree_is_a_clean_no_op(tmp_path: Path) -> None:
    """A fresh install has no blobs/ dir; daemon startup must not raise."""
    result = sweep_blob_temps(tmp_path / "never-created", max_age_s=1, now=1.0)
    assert result == SweepResult(scanned=0, removed=0, bytes_reclaimed=0, errors=0)


def test_a_vanished_temp_is_not_an_error(tmp_path: Path) -> None:
    """Two sweeps racing (CLI + daemon startup) must not blow up on the loser."""
    now = 1_000_000.0
    d = _blob_dir(tmp_path, SHA)
    gone = d / temp_name(SHA, pid=1, token=uuid.uuid4().hex)
    gone.write_bytes(b"a")
    _age(gone, 90_000, now=now)

    real_unlink = Path.unlink

    def racing_unlink(self: Path, *a: object, **k: object) -> None:
        real_unlink(self)
        raise FileNotFoundError(str(self))

    Path.unlink = racing_unlink  # type: ignore[method-assign]
    try:
        result = sweep_blob_temps(tmp_path, max_age_s=86_400, now=now)
    finally:
        Path.unlink = real_unlink  # type: ignore[method-assign]

    assert not gone.exists()
    assert result.errors == 0
    assert result.removed == 1


def test_a_temp_that_vanishes_before_it_is_judged_is_not_claimed_as_removed(
    tmp_path: Path,
) -> None:
    """`stat` runs before the age check, so its FileNotFoundError proves nothing.

    The file may have been days old or seconds old; the sweep never found out
    and did not delete it. Counting it as removed inflates the report, and under
    `--dry-run` — whose entire job is to say what *would* happen — it claims an
    intent the sweep never formed.
    """
    now = 1_000_000.0
    d = _blob_dir(tmp_path, SHA)
    young = d / temp_name(SHA, pid=1, token=uuid.uuid4().hex)
    young.write_bytes(b"still being written")
    _age(young, 1, now=now)

    real_stat = Path.stat

    def vanishing_stat(self: Path, **k: object):
        if self.name.endswith(".tmp"):
            raise FileNotFoundError(str(self))
        return real_stat(self, **k)

    Path.stat = vanishing_stat  # type: ignore[method-assign]
    try:
        result = sweep_blob_temps(tmp_path, max_age_s=86_400, now=now, dry_run=True)
    finally:
        Path.stat = real_stat  # type: ignore[method-assign]

    assert result.removed == 0
    assert result.errors == 0


# --- wiring: config, writer, daemon startup, CLI ---------------------------


def test_temp_max_age_default_is_a_generous_config_knob() -> None:
    """No literal in sweep code — the age gate is operator-tunable (#237)."""
    from localmail.config import AttachmentsConfig

    cfg = AttachmentsConfig()
    assert cfg.temp_max_age_s == 86_400
    assert AttachmentsConfig(temp_max_age_s=60).temp_max_age_s == 60


def test_the_age_gate_cannot_be_configured_away(tmp_path: Path) -> None:
    """The gate is the *only* thing protecting a live writer's temp.

    `is_expired` is `now - mtime > max_age_s`, so at zero a temp written
    microseconds ago is already expired and the sweep deletes a file whose
    writer is between `write_bytes` and `replace` — turning a healthy message
    into a `failed_messages` poison pill. Nothing legitimate needs a
    sub-second gate, so the floor costs nothing.
    """
    import pydantic

    from localmail.config import AttachmentsConfig

    for bad in (0, -1):
        with pytest.raises(pydantic.ValidationError):
            AttachmentsConfig(temp_max_age_s=bad)


def test_cli_rejects_a_non_positive_age_gate(tmp_path: Path, cli_config) -> None:
    from click.testing import CliRunner

    from localmail.cli import main

    result = CliRunner().invoke(
        main,
        ["--config", str(cli_config), "sweep-blob-temps", "--max-age-seconds", "0"],
    )
    assert result.exit_code != 0


def test_a_temp_stranded_by_a_hard_kill_is_collected(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: the writer's own staged name is one the sweeper recognises.

    Simulates SIGKILL between `write_bytes` and `replace` — the one failure the
    writer's `except BaseException: unlink` cannot cover. Without a shared
    minter/matcher module, a rename of the temp format in `attachments.py`
    would silently strand every future orphan; this fails if they drift.
    """
    import localmail.attachments as attachments
    from localmail.parser import Attachment, ParsedMessage

    # A hard kill runs NO cleanup — not even the writer's own
    # `except BaseException: tmp.unlink()`. Raising here would therefore
    # simulate the wrong thing (that path is already covered); a `replace`
    # that never happens leaves exactly the on-disk state SIGKILL leaves.
    monkeypatch.setattr(Path, "replace", lambda self, target: None)

    parsed = ParsedMessage(
        message_id="<m@x>", raw_sha256=b"\x00" * 32, in_reply_to=None, refs=[],
        subject="s", from_addr=None, from_name=None, to_addrs=[], cc_addrs=[],
        bcc_addrs=[], date_sent=None, headers={}, body_text=None,
        body_html=None, raw_bytes=b"raw", size_bytes=3,
        attachments=[Attachment(filename="a.txt", mime_type="text/plain",
                                payload=b"hello")],
    )

    class _Cur:
        def execute(self, *a: object, **k: object) -> None: ...
        def __enter__(self) -> "_Cur":
            return self
        def __exit__(self, *a: object) -> None: ...

    class _Conn:
        def cursor(self) -> "_Cur":
            return _Cur()

    attachments.write_attachments(_Conn(), parsed, root=tmp_path)  # type: ignore[arg-type]
    monkeypatch.undo()

    stranded = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert len(stranded) == 1, f"expected one stranded temp, got {stranded}"
    now = 1_000_000.0
    _age(stranded[0], 90_000, now=now)

    result = sweep_blob_temps(tmp_path, max_age_s=86_400, now=now)

    assert result.removed == 1
    assert not stranded[0].exists()


def test_daemon_startup_sweeps_stale_temps(tmp_path: Path, db_dsn, monkeypatch) -> None:
    """Opportunistic collection at startup, like `reconcile_orphaned_jobs`."""
    import localmail.daemon as daemon_mod
    from localmail.config import LocalmailConfig
    from localmail.daemon import Daemon

    d = _blob_dir(tmp_path, SHA)
    old = d / temp_name(SHA, pid=1, token=uuid.uuid4().hex)
    old.write_bytes(b"x" * 5)
    os.utime(old, (0.0, 0.0))  # epoch: expired under any sane gate

    cfg = LocalmailConfig.model_validate({
        "database": {"dsn": db_dsn},
        "attachments": {"root": str(tmp_path)},
    })
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    monkeypatch.setattr(daemon_mod, "list_syncable_accounts", lambda conn: [])

    daemon = Daemon(cfg=cfg, dsn=db_dsn)
    try:
        daemon.start_workers()
        assert not old.exists()
    finally:
        daemon.stop()
        daemon.join(timeout=2)
        daemon.pool.close()


def test_daemon_startup_sweep_logs_before_and_after(
    tmp_path: Path, db_dsn, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#269: the sweep can take minutes on a cold cache, with the previous log
    line being `pool sizing` — an operator (or monitoring) reading an empty
    `daemon_heartbeats` then sees a daemon that looks dead. One INFO line
    *before* the walk names what is happening; one *after* reports the counts
    unconditionally (the old line was skipped when nothing was removed, i.e.
    on exactly the silent-but-slow startups the issue is about)."""
    import localmail.daemon as daemon_mod
    from localmail.config import LocalmailConfig
    from localmail.daemon import Daemon

    cfg = LocalmailConfig.model_validate({
        "database": {"dsn": db_dsn},
        "attachments": {"root": str(tmp_path)},
    })
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    monkeypatch.setattr(daemon_mod, "list_syncable_accounts", lambda conn: [])

    daemon = Daemon(cfg=cfg, dsn=db_dsn)
    try:
        with caplog.at_level("INFO", logger="localmail.daemon"):
            daemon.start_workers()
    finally:
        daemon.stop()
        daemon.join(timeout=2)
        daemon.pool.close()

    messages = [r.getMessage() for r in caplog.records]
    before = [i for i, m in enumerate(messages) if "sweeping blob temps" in m]
    after = [i for i, m in enumerate(messages) if "blob-temp sweep done" in m]
    assert len(before) == 1, messages
    assert len(after) == 1, messages
    assert before[0] < after[0]
    assert str(tmp_path) in messages[before[0]]
    # Unconditional counts, even on a no-op sweep over an empty tree.
    assert "scanned=0" in messages[after[0]]
    assert "removed=0" in messages[after[0]]


def test_cli_exposes_sweep_blob_temps_with_dry_run() -> None:
    from click.testing import CliRunner

    from localmail.cli import main

    result = CliRunner().invoke(main, ["sweep-blob-temps", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "--max-age-seconds" in result.output


def test_cli_sweep_reports_and_honours_dry_run(tmp_path: Path, cli_config) -> None:
    from click.testing import CliRunner

    from localmail.cli import main

    d = _blob_dir(tmp_path, SHA)
    old = d / temp_name(SHA, pid=1, token=uuid.uuid4().hex)
    old.write_bytes(b"x" * 9)
    os.utime(old, (0.0, 0.0))

    cli_config.write_text(
        cli_config.read_text() + f'\n[attachments]\nroot = "{tmp_path}"\n'
    )
    runner = CliRunner()
    args = ["--config", str(cli_config), "sweep-blob-temps"]

    dry = runner.invoke(main, [*args, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "1" in dry.output and old.exists()

    real = runner.invoke(main, args)
    assert real.exit_code == 0, real.output
    assert not old.exists()
