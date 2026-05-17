"""End-to-end smoke: spin up uvicorn in a thread, hit it with httpx, verify."""
import socket
import threading
import time
from pathlib import Path

import httpx
import psycopg
import pytest
import uvicorn

from localmail.api.auth import create_user, reset_login_rate_limiter
from localmail.serve.app import create_app
from localmail.serve.tls import ensure_self_signed_cert


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"server did not come up on port {port}")


@pytest.mark.integration
def test_e2e_login_capabilities(db_dsn: str, tmp_path: Path) -> None:
    reset_login_rate_limiter()
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="localhost")

    with psycopg.connect(db_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_users WHERE username = 'alice'")
        conn.commit()
        create_user(conn, "alice", "hunter2")
        conn.commit()

    app = create_app(db_dsn=db_dsn, searcher=None)
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        ssl_certfile=str(cert), ssl_keyfile=str(key),
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_port(port)
        base = f"https://127.0.0.1:{port}"
        with httpx.Client(verify=False, base_url=base, timeout=5.0) as c:
            r = c.get("/v1/version")
            assert r.status_code == 200

            r = c.post("/v1/auth/login", json={"username": "alice", "password": "hunter2"})
            assert r.status_code == 200, r.text
            tok = r.json()["token"]

            r = c.get("/v1/capabilities", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200
            assert r.json()["search"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        # Surface a thread that didn't drain — otherwise we leak it to the
        # next test which can race against the port still being in TIME_WAIT.
        assert not thread.is_alive(), "uvicorn server thread did not exit within 5s"
