# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from localmail.serve.tls import (
    cert_fingerprint_sha256_hex,
    ensure_self_signed_cert,
)


def _load(cert_path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(cert_path.read_bytes())


def _san(cert: x509.Certificate) -> x509.SubjectAlternativeName:
    return cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value


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


# --- robustness for a fresh WireGuard/IP deployment ----------------------------
# The kastellan egress proxy re-originates TLS with rustls-webpki, which matches
# an IP connection only against an IPAddress SAN — never a DNSName holding an IP
# literal. That SAN typing is the behavioural fix. The BasicConstraints / EKU /
# KeyUsage assertions below pin extensions that were previously absent
# altogether, so a later edit can't silently drop the explicit server-leaf shape
# stricter validators want.


def test_ip_hostname_becomes_an_ip_san_not_a_dns_san(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="10.0.0.3")
    san = _san(_load(cert))
    assert ipaddress.ip_address("10.0.0.3") in san.get_values_for_type(x509.IPAddress)
    assert "10.0.0.3" not in san.get_values_for_type(x509.DNSName)


def test_cert_is_a_non_ca_leaf(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="10.0.0.3")
    bc = _load(cert).extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False


def test_cert_has_server_auth_eku(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="10.0.0.3")
    eku = _load(cert).extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku


def test_cert_has_digital_signature_key_usage(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="10.0.0.3")
    ku = _load(cert).extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.digital_signature is True
    assert ku.key_cert_sign is False


def test_cert_always_covers_loopback_and_keeps_dns_names(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="mail.wg.example")
    san = _san(_load(cert))
    assert ipaddress.ip_address("127.0.0.1") in san.get_values_for_type(x509.IPAddress)
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    # a genuine DNS hostname stays a DNSName
    assert "mail.wg.example" in san.get_values_for_type(x509.DNSName)
