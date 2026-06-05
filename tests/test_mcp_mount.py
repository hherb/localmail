"""create_app mounts /mcp only when enabled (and the extra is importable)."""
from starlette.routing import Mount

from localmail.config import McpConfig
from localmail.serve.app import create_app


def _has_mcp_mount(app) -> bool:
    return any(
        isinstance(r, Mount) and r.path.rstrip("/") == "/mcp" for r in app.routes
    )


def test_mcp_not_mounted_by_default(db_dsn):
    app = create_app(db_dsn=db_dsn)
    assert not _has_mcp_mount(app)


def test_mcp_mounted_when_enabled(db_dsn):
    app = create_app(db_dsn=db_dsn, enable_mcp=True, mcp_config=McpConfig(enabled=True))
    assert _has_mcp_mount(app)
