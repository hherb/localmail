from pathlib import Path

from click.testing import CliRunner

from localmail.cli import main


def test_rotate_tls_writes_new_cert(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    runner = CliRunner()
    r = runner.invoke(main, ["rotate-tls", "--cert", str(cert), "--key", str(key)])
    assert r.exit_code == 0, r.output
    assert cert.exists()
    assert key.exists()
    cert_bytes = cert.read_bytes()
    r2 = runner.invoke(main, ["rotate-tls", "--cert", str(cert), "--key", str(key), "--force"])
    assert r2.exit_code == 0
    assert cert.read_bytes() != cert_bytes
