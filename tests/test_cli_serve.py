# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import logging
import threading
import time

from click.testing import CliRunner

from localmail.cli import _warm_reranker_in_background, main


def test_serve_help() -> None:
    runner = CliRunner()
    r = runner.invoke(main, ["serve", "--help"])
    assert r.exit_code == 0
    assert "--bind" in r.output
    assert "--port" in r.output
    assert "--tls-cert" in r.output
    assert "--no-tls" in r.output


def test_warm_reranker_runs_in_background_and_does_not_block(caplog):
    """The warmup must return immediately (background thread) and log
    success once the reranker call completes."""
    started = threading.Event()
    finished = threading.Event()

    class _SlowReranker:
        name = "fastembed"
        model = "stub/slow"

        def rerank(self, query, candidates):
            started.set()
            time.sleep(0.05)
            finished.set()
            return [0.5] * len(candidates)

    rr = _SlowReranker()
    with caplog.at_level(logging.INFO, logger="localmail.search"):
        t0 = time.monotonic()
        _warm_reranker_in_background(rr)
        # Must return immediately — way under the 50ms the stub sleeps for.
        assert (time.monotonic() - t0) < 0.02
        assert started.wait(timeout=2.0), "warmup thread never started"
        assert finished.wait(timeout=2.0), "warmup thread never finished"
        # Give the logger a beat to flush after the thread returned.
        for _ in range(20):
            if any("warm in" in rec.message for rec in caplog.records):
                break
            time.sleep(0.01)
    assert any("warming reranker" in rec.message for rec in caplog.records)
    assert any("warm in" in rec.message for rec in caplog.records)


def test_warm_reranker_swallows_failures(caplog):
    """A broken reranker must not crash the warmup thread — it logs and
    returns so the serve process keeps running."""
    class _BrokenReranker:
        name = "fastembed"
        model = "stub/broken"

        def rerank(self, query, candidates):
            raise RuntimeError("onnx session init failed")

    with caplog.at_level(logging.WARNING, logger="localmail.search"):
        _warm_reranker_in_background(_BrokenReranker())
        # Join via polling for the warning to land.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any("warmup failed" in rec.message for rec in caplog.records):
                break
            time.sleep(0.01)
    assert any("warmup failed" in rec.message for rec in caplog.records)
