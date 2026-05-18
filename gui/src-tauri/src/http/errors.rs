//! Typed HTTP errors returned by the http::client helpers and ultimately
//! surfaced to the JS side via serde.

use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum HttpError {
    #[error("network error: {0}")]
    Network(String),

    #[error("TLS certificate did not match pinned fingerprint")]
    CertMismatch,

    #[error("server returned HTTP {status}: {body}")]
    HttpStatus { status: u16, body: String },

    #[error("could not parse server response as JSON: {0}")]
    Decode(String),

    #[error("invalid server URL: {0}")]
    BadUrl(String),

    #[error("request timed out after {seconds}s")]
    Timeout { seconds: u64 },
}

impl HttpError {
    pub fn from_reqwest(err: reqwest::Error, timeout_secs: u64) -> Self {
        if err.is_timeout() {
            Self::Timeout {
                seconds: timeout_secs,
            }
        } else if err.is_decode() {
            Self::Decode(err.to_string())
        } else if is_rustls_cert_error(&err) {
            Self::CertMismatch
        } else {
            Self::Network(err.to_string())
        }
    }
}

fn is_rustls_cert_error(err: &reqwest::Error) -> bool {
    let mut current: Option<&(dyn std::error::Error + 'static)> = Some(err);
    while let Some(e) = current {
        if let Some(rustls_err) = e.downcast_ref::<rustls::Error>() {
            return matches!(rustls_err, rustls::Error::InvalidCertificate(_));
        }
        current = e.source();
    }
    false
}
