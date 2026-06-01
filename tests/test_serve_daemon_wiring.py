"""create_app builds the right supervisor on app.state (2B.4)."""
from __future__ import annotations

import pytest

from localmail.config import DaemonConfig, ServeConfig
from localmail.serve.app import create_app
from localmail.serve.daemon_supervisor import (
    DaemonSupervisor,
    ExternalDaemonSupervisor,
)


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(cookie_secure=False)


def test_supervisor_is_real_when_supervising(db_dsn, serve_cfg) -> None:
    app = create_app(
        db_dsn=db_dsn,
        serve_config=serve_cfg.model_copy(update={"supervise_daemon": True}),
    )
    assert isinstance(app.state.daemon_supervisor, DaemonSupervisor)


def test_supervisor_is_stub_when_external(db_dsn, serve_cfg) -> None:
    app = create_app(
        db_dsn=db_dsn,
        serve_config=serve_cfg.model_copy(update={"supervise_daemon": False}),
    )
    assert isinstance(app.state.daemon_supervisor, ExternalDaemonSupervisor)


def test_daemon_config_defaults_present(db_dsn, serve_cfg) -> None:
    app = create_app(db_dsn=db_dsn, serve_config=serve_cfg)
    assert isinstance(app.state.daemon_config, DaemonConfig)


def test_creating_app_does_not_bind_control_socket(db_dsn, serve_cfg) -> None:
    """create_app alone is side-effect-free: the control socket is bound only
    by the lifespan when serving with the socket enabled, not at construction."""
    app = create_app(db_dsn=db_dsn, serve_config=serve_cfg)
    assert getattr(app.state, "control_socket_server", None) is None
