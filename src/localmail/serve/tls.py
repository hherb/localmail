# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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


def _build_cert(hostname: str) -> tuple[bytes, bytes]:
    """Return (cert_pem, key_pem) for a fresh self-signed ECDSA P-256 pair."""
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
    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_bytes


def ensure_self_signed_cert(*, cert_path: Path, key_path: Path, hostname: str) -> None:
    """Create a fresh ECDSA P-256 self-signed cert + key pair if absent.

    Idempotent: if both files exist, does nothing.
    """
    if cert_path.exists() and key_path.exists():
        return

    cert_pem, key_bytes = _build_cert(hostname)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(key_path, key_bytes, 0o600)
    _write_atomic(cert_path, cert_pem, 0o644)


def rotate_self_signed_cert(*, cert_path: Path, key_path: Path, hostname: str) -> None:
    """Atomically replace an existing cert+key pair with a fresh one.

    Writes the new cert and key to sibling temp files first, then renames them
    over the originals (``os.replace``) so a running ``localmail serve``
    process is never left looking at a missing or half-written cert. The temp
    files are created with the final permissions (0o600 on the key, 0o644 on
    the cert) before any bytes are written, mirroring ``_write_atomic``.

    The rename swaps the inode atomically; existing file descriptors held by a
    running server keep pointing at the old contents until the process is
    restarted. This is intentional — TLS sessions in progress are not torn
    down by the rotation.
    """
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    cert_pem, key_bytes = _build_cert(hostname)

    cert_tmp = cert_path.with_name(cert_path.name + ".new")
    key_tmp = key_path.with_name(key_path.name + ".new")
    for stale in (cert_tmp, key_tmp):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    _write_atomic(key_tmp, key_bytes, 0o600)
    try:
        _write_atomic(cert_tmp, cert_pem, 0o644)
    except Exception:
        try:
            key_tmp.unlink()
        except FileNotFoundError:
            pass
        raise

    os.replace(key_tmp, key_path)
    os.replace(cert_tmp, cert_path)


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
