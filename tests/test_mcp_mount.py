# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""create_app mounts /mcp only when enabled (and the extra is importable)."""
import pytest
from starlette.routing import Mount

from localmail.config import McpConfig
from localmail.serve.app import create_app

# The `enabled` mount needs the mcp SDK to build the server; skip the whole
# module when the [mcp] extra is absent (the mount is a no-op without it).
pytest.importorskip("mcp")


def _has_mcp_mount(app) -> bool:
    return any(
        isinstance(r, Mount) and r.path.rstrip("/") == "/mcp" for r in app.routes
    )


def test_mcp_not_mounted_by_default(db_dsn):
    app = create_app(db_dsn=db_dsn)
    try:
        assert not _has_mcp_mount(app)
    finally:
        app.state.pool.close()


def test_mcp_mounted_when_enabled(db_dsn):
    app = create_app(db_dsn=db_dsn, enable_mcp=True, mcp_config=McpConfig(enabled=True))
    try:
        assert _has_mcp_mount(app)
    finally:
        app.state.pool.close()
