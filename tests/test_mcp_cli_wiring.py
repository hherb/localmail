"""`localmail serve` threads cfg.mcp into create_app."""
from click.testing import CliRunner

from localmail.cli import main


def _patch_serve_runtime(monkeypatch, captured):
    """Stub the heavy bits of serve_cmd so we can capture create_app kwargs."""
    import localmail.serve.app as serve_app
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
    def _no_search():
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
