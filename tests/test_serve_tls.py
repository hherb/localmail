from pathlib import Path

from localmail.serve.tls import (
    cert_fingerprint_sha256_hex,
    ensure_self_signed_cert,
)


def test_ensure_self_signed_cert_creates_both_files(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="localhost")
    assert cert.exists()
    assert key.exists()
    assert cert.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert key.read_text().startswith("-----BEGIN ")


def test_ensure_self_signed_cert_is_idempotent(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="localhost")
    cert_bytes = cert.read_bytes()
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="localhost")
    assert cert.read_bytes() == cert_bytes


def test_cert_fingerprint_is_64_hex_chars(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="localhost")
    fp = cert_fingerprint_sha256_hex(cert_path=cert)
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)
