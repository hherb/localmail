"""Test that Daemon spawns + cleanly joins the extract_worker thread."""

from __future__ import annotations

import threading
import time

from localmail.config import LocalmailConfig
from localmail.daemon import Daemon


class _E:
    name = "s"; model = "s"; dimension = 768

    def embed_documents(self, t):
        return [[0.5] * 768 for _ in t]

    def embed_query(self, t):
        return [0.5] * 768

    def health_check(self) -> None:
        pass


def test_daemon_starts_extract_worker_when_enabled(db_dsn) -> None:
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_extract_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    time.sleep(0.5)
    names = {t.name for t in threading.enumerate()}
    assert any(n.startswith("extract_worker") for n in names)
    d.stop()
    d.join(timeout=5)
    names_after = {t.name for t in threading.enumerate()}
    assert not any(n.startswith("extract_worker") for n in names_after)


def test_daemon_skips_extract_worker_when_disabled(db_dsn) -> None:
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_extract_worker = False
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    time.sleep(0.3)
    names = {t.name for t in threading.enumerate()}
    assert not any(n.startswith("extract_worker") for n in names)
    d.stop()
    d.join(timeout=5)
