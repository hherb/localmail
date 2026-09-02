# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`localmail serve` threads cfg.mcp into create_app."""
from click.testing import CliRunner

from localmail.cli import main

# Module scope, not function scope: the autouse pool-closing fixture reads
# sys.modules at test-setup time (#321, tests/_pool_leaks.py).
import localmail.serve.app as serve_app


def _patch_serve_runtime(monkeypatch, captured):
    """Stub the heavy bits of serve_cmd so we can capture create_app kwargs."""
    import localmail.db as db_mod
    import localmail.search as search_mod
    import uvicorn

    def fake_create_app(**kwargs):
        captured.update(kwargs)

        class _Dummy:
            pass
        return _Dummy()

    monkeypatch.setattr(serve_app, "create_app", fake_create_app)
    monkeypatch.setattr(db_mod, "pending_migrations", lambda dsn: [])
    def _no_search(*a, **k):
        raise RuntimeError("search disabled in test")
    monkeypatch.setattr(search_mod, "create_searcher", _no_search)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)


def test_serve_enables_mcp_from_config(monkeypatch, tmp_path, db_dsn):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_dsn}"\n\n[mcp]\nenabled = true\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))
    monkeypatch.delenv("LOCALMAIL_DSN_OVERRIDE", raising=False)
    captured: dict = {}
    _patch_serve_runtime(monkeypatch, captured)

    result = CliRunner().invoke(
        main, ["--config", str(cfg), "serve", "--no-tls", "--bind", "127.0.0.1"])
    assert result.exit_code == 0, result.output
    assert captured["enable_mcp"] is True
    assert captured["mcp_config"].enabled is True


def test_serve_mcp_disabled_by_default(monkeypatch, tmp_path, db_dsn):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))
    monkeypatch.delenv("LOCALMAIL_DSN_OVERRIDE", raising=False)
    captured: dict = {}
    _patch_serve_runtime(monkeypatch, captured)

    result = CliRunner().invoke(
        main, ["--config", str(cfg), "serve", "--no-tls", "--bind", "127.0.0.1"])
    assert result.exit_code == 0, result.output
    assert captured["enable_mcp"] is False


def test_serve_searcher_uses_override_dsn(monkeypatch, tmp_path, db_dsn):
    """The searcher must query the same DB serve does, incl. the DSN override.

    Regression: in the LOCALMAIL_DSN_OVERRIDE branch serve called
    create_searcher(None), which loaded the *default* config and used its DSN —
    so the searcher could silently query a different database than serve itself.
    serve must pass the override DSN through.
    """
    import localmail.db as db_mod
    import localmail.search as search_mod
    import uvicorn

    monkeypatch.setattr(serve_app, "create_app", lambda **k: object())
    monkeypatch.setattr(db_mod, "pending_migrations", lambda dsn: [])
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv("LOCALMAIL_DSN_OVERRIDE", db_dsn)

    seen: dict = {}

    def fake_create(cfg=None, *, dsn=None, **kw):
        seen["cfg"] = cfg
        seen["dsn"] = dsn
        raise RuntimeError("stop after capturing")

    monkeypatch.setattr(search_mod, "create_searcher", fake_create)

    result = CliRunner().invoke(main, ["serve", "--no-tls", "--bind", "127.0.0.1"])
    assert result.exit_code == 0, result.output
    assert seen["cfg"] is None, "override branch should pass cfg=None"
    assert seen["dsn"] == db_dsn, "searcher must receive the override DSN"
