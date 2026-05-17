//! TOFU certificate verifier.
//!
//! Replaces rustls' built-in chain validation with a per-instance policy:
//!
//! - `TofuMode::Probe`: accept any cert. The verifier records the leaf
//!   certificate's SHA-256 fingerprint as it goes by, exposed via
//!   `TofuVerifier::captured_fingerprint()`. Used during first-run
//!   connection so the user can confirm the pin.
//!
//! - `TofuMode::Pinned(sha256)`: accept ONLY if the leaf cert's SHA-256
//!   matches `sha256`. Anything else returns `TlsError::InvalidCertificate`.
//!
//! Neither mode performs hostname verification, expiry checking, or chain
//! validation. The pin is what authenticates the server — a TOFU pin is
//! all-or-nothing.

use std::sync::{Arc, Mutex};

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::{DigitallySignedStruct, Error as TlsError, SignatureScheme};
use rustls_pki_types::{CertificateDer, ServerName, UnixTime};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone)]
pub enum TofuMode {
    /// Accept any certificate; record the leaf's SHA-256 for later confirmation.
    Probe,
    /// Accept only certificates whose leaf SHA-256 matches `expected_hex`.
    Pinned { expected_hex: String },
}

#[derive(Debug)]
pub struct TofuVerifier {
    mode: TofuMode,
    captured: Mutex<Option<String>>,
}

impl TofuVerifier {
    pub fn new(mode: TofuMode) -> Arc<Self> {
        Arc::new(Self { mode, captured: Mutex::new(None) })
    }

    /// Returns the SHA-256 (lowercase hex) of the leaf cert seen during the
    /// last successful handshake. `None` until `verify_server_cert` has been
    /// called at least once.
    pub fn captured_fingerprint(&self) -> Option<String> {
        self.captured.lock().unwrap().clone()
    }
}

impl ServerCertVerifier for TofuVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, TlsError> {
        let mut h = Sha256::new();
        h.update(end_entity.as_ref());
        let digest = h.finalize();
        let fingerprint: String = digest.iter().map(|b| format!("{:02x}", b)).collect();

        *self.captured.lock().unwrap() = Some(fingerprint.clone());

        match &self.mode {
            TofuMode::Probe => Ok(ServerCertVerified::assertion()),
            TofuMode::Pinned { expected_hex } => {
                if fingerprint.eq_ignore_ascii_case(expected_hex) {
                    Ok(ServerCertVerified::assertion())
                } else {
                    Err(TlsError::InvalidCertificate(
                        rustls::CertificateError::ApplicationVerificationFailure,
                    ))
                }
            }
        }
    }

    fn verify_tls12_signature(
        &self,
        _message: &[u8],
        _cert: &CertificateDer<'_>,
        _dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _message: &[u8],
        _cert: &CertificateDer<'_>,
        _dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        vec![
            SignatureScheme::RSA_PKCS1_SHA256,
            SignatureScheme::RSA_PKCS1_SHA384,
            SignatureScheme::RSA_PKCS1_SHA512,
            SignatureScheme::ECDSA_NISTP256_SHA256,
            SignatureScheme::ECDSA_NISTP384_SHA384,
            SignatureScheme::ECDSA_NISTP521_SHA512,
            SignatureScheme::RSA_PSS_SHA256,
            SignatureScheme::RSA_PSS_SHA384,
            SignatureScheme::RSA_PSS_SHA512,
            SignatureScheme::ED25519,
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rcgen::generate_simple_self_signed;

    fn make_cert_der() -> CertificateDer<'static> {
        let ck = generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        CertificateDer::from(ck.cert.der().to_vec())
    }

    fn make_other_cert_der() -> CertificateDer<'static> {
        let ck = generate_simple_self_signed(vec!["other.example".into()]).unwrap();
        CertificateDer::from(ck.cert.der().to_vec())
    }

    fn sha256_hex(der: &[u8]) -> String {
        let mut h = Sha256::new();
        h.update(der);
        let out = h.finalize();
        out.iter().map(|b| format!("{:02x}", b)).collect()
    }

    fn server_name() -> ServerName<'static> {
        ServerName::try_from("localhost").unwrap()
    }

    fn now() -> UnixTime {
        UnixTime::now()
    }

    #[test]
    fn probe_mode_accepts_any_cert_and_captures_fingerprint() {
        let cert = make_cert_der();
        let expected = sha256_hex(cert.as_ref());

        let v = TofuVerifier::new(TofuMode::Probe);
        let result = v.verify_server_cert(&cert, &[], &server_name(), &[], now());
        assert!(result.is_ok(), "Probe should accept any cert, got {:?}", result.err());

        assert_eq!(v.captured_fingerprint().as_deref(), Some(expected.as_str()));
    }

    #[test]
    fn pinned_mode_accepts_matching_fingerprint() {
        let cert = make_cert_der();
        let pin = sha256_hex(cert.as_ref());

        let v = TofuVerifier::new(TofuMode::Pinned { expected_hex: pin });
        let result = v.verify_server_cert(&cert, &[], &server_name(), &[], now());
        assert!(result.is_ok(), "Pinned matching should accept, got {:?}", result.err());
    }

    #[test]
    fn pinned_mode_rejects_non_matching_fingerprint() {
        let cert = make_cert_der();
        let other = make_other_cert_der();
        let pin = sha256_hex(cert.as_ref());

        let v = TofuVerifier::new(TofuMode::Pinned { expected_hex: pin });
        let result = v.verify_server_cert(&other, &[], &server_name(), &[], now());
        assert!(result.is_err(), "Pinned non-matching should reject");
        match result {
            Err(TlsError::InvalidCertificate(_)) => (),
            other => panic!("Expected InvalidCertificate, got {:?}", other),
        }
    }

    #[test]
    fn fingerprint_capture_is_lowercase_hex_64_chars() {
        let cert = make_cert_der();
        let v = TofuVerifier::new(TofuMode::Probe);
        v.verify_server_cert(&cert, &[], &server_name(), &[], now()).unwrap();
        let fp = v.captured_fingerprint().unwrap();
        assert!(fp.chars().all(|c| c.is_ascii_hexdigit() && (c.is_numeric() || c.is_ascii_lowercase())));
        assert_eq!(fp.len(), 64);
    }
}
