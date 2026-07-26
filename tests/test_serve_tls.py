# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID

from localmail.serve.tls import (
    cert_fingerprint_sha256_hex,
    ensure_self_signed_cert,
)


def _load(cert_path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(cert_path.read_bytes())


def _san(cert: x509.Certificate) -> x509.SubjectAlternativeName:
    return cert.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
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
# The kastellan egress proxy re-originates TLS with rustls-webpki, which is
# stricter than openssl: an IP connection matches only an IPAddress SAN (never a
# DNSName), and a self-signed CA cert served as the leaf is rejected
# (CaUsedAsEndEntity). The generated cert must therefore be a non-CA server leaf
# with correctly-typed SANs.


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
    bc = _load(cert).extensions.get_extension_for_oid(
        ExtensionOID.BASIC_CONSTRAINTS
    ).value
    assert bc.ca is False


def test_cert_has_server_auth_eku(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="10.0.0.3")
    eku = _load(cert).extensions.get_extension_for_oid(
        ExtensionOID.EXTENDED_KEY_USAGE
    ).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku


def test_cert_always_covers_loopback_and_keeps_dns_names(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert_path=cert, key_path=key, hostname="mail.wg.example")
    san = _san(_load(cert))
    assert ipaddress.ip_address("127.0.0.1") in san.get_values_for_type(x509.IPAddress)
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    # a genuine DNS hostname stays a DNSName
    assert "mail.wg.example" in san.get_values_for_type(x509.DNSName)
