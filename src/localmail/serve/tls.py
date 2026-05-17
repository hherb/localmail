"""Self-signed certificate generation + fingerprint computation for TOFU pinning."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_CERT_TTL_DAYS = 365 * 10


def ensure_self_signed_cert(*, cert_path: Path, key_path: Path, hostname: str) -> None:
    """Create a fresh ECDSA P-256 self-signed cert + key pair if absent.

    Idempotent: if both files exist, does nothing.
    """
    if cert_path.exists() and key_path.exists():
        return

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "localmail"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=_CERT_TTL_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname), x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _write_atomic(key_path, key_bytes, 0o600)
    _write_atomic(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Create-or-fail write that sets file mode atomically.

    Using write_bytes + chmod leaves a window where the file exists at the
    process umask. O_EXCL guarantees we don't truncate an existing key, and
    os.open's mode argument is applied before any data is written.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def cert_fingerprint_sha256_hex(*, cert_path: Path) -> str:
    """Return the SHA-256 fingerprint of the leaf certificate (DER)."""
    pem = cert_path.read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()
