import stat
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


def test_rotate_tls_key_is_user_only_readable(tmp_path: Path) -> None:
    """The TLS private key must never be world-readable."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    runner = CliRunner()
    r = runner.invoke(main, ["rotate-tls", "--cert", str(cert), "--key", str(key)])
    assert r.exit_code == 0, r.output
    mode = stat.S_IMODE(key.stat().st_mode)
    assert mode == 0o600, f"key mode {oct(mode)} != 0o600"


def test_rotate_tls_force_replaces_atomically(tmp_path: Path) -> None:
    """--force must never leave the filesystem in a half-written state.

    Concretely: after a successful rotation, both files must exist and be
    parseable as a valid PEM cert+key pair; no `.new` siblings should remain;
    and the inode of the key file should have changed (it was replaced via
    os.replace, not truncated in place).
    """
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    runner = CliRunner()
    r1 = runner.invoke(main, ["rotate-tls", "--cert", str(cert), "--key", str(key)])
    assert r1.exit_code == 0, r1.output
    inode_before = key.stat().st_ino

    r2 = runner.invoke(
        main, ["rotate-tls", "--cert", str(cert), "--key", str(key), "--force"],
    )
    assert r2.exit_code == 0, r2.output
    assert cert.exists() and key.exists()
    assert not (tmp_path / "cert.pem.new").exists()
    assert not (tmp_path / "key.pem.new").exists()
    assert key.stat().st_ino != inode_before

    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.x509 import load_pem_x509_certificate

    load_pem_x509_certificate(cert.read_bytes())
    load_pem_private_key(key.read_bytes(), password=None)


def test_rotate_tls_without_force_is_idempotent(tmp_path: Path) -> None:
    """Re-running without --force on an existing pair leaves them untouched."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    runner = CliRunner()
    r1 = runner.invoke(main, ["rotate-tls", "--cert", str(cert), "--key", str(key)])
    assert r1.exit_code == 0, r1.output
    cert_first = cert.read_bytes()
    key_first = key.read_bytes()
    r2 = runner.invoke(main, ["rotate-tls", "--cert", str(cert), "--key", str(key)])
    assert r2.exit_code == 0, r2.output
    assert cert.read_bytes() == cert_first
    assert key.read_bytes() == key_first
