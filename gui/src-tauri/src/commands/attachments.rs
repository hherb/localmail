//! Attachment download.
//!
//! GET /v1/attachments/{sha256} → read the body (bounded, see
//! [`crate::http::body::read_capped`]), write it to disk without ever leaving
//! a truncated file ([`crate::save_file::write_replacing`]), return byte count.
//! Caller (Svelte) provides the destination path obtained from a save dialog.
//!
//! Errors surface via the dedicated [`AttachmentError`] enum so the auth-domain
//! `AuthError` no longer carries a catch-all `Io` variant — issue #22 split
//! these out so a `formatError()` consumer on the JS side never has to ask
//! "is this auth or attachment?". Auth pre-check failures still compose by
//! bubbling through `AttachmentError::Auth(#[from] AuthError)`.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use thiserror::Error;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::body::{error_chain, read_capped, BodyError};
use crate::http::client::build_pinned_client;
use crate::http::client::REQUEST_TIMEOUT_SECS;
use crate::http::errors::HttpError;
use crate::save_file::write_replacing;
use crate::storage::keyring::KeyringStore;

// Hard ceiling on a single attachment payload. The server is trusted (pinned
// TLS, our own deployment), but a misconfigured or compromised response
// without this guard would buffer the entire body into memory — and for
// `fetch_attachment_bytes`, then re-serialize it as Vec<u8> over IPC.
const MAX_ATTACHMENT_BYTES: u64 = 100 * 1024 * 1024;

/// Errors raised by attachment commands.
///
/// Distinct from [`AuthError`] so a `formatError()` consumer on the JS side
/// can distinguish auth-domain failures (`NotConnected`, `NotLoggedIn`,
/// `Keyring`) from attachment-domain ones (`InvalidSha256`, `TooLarge`,
/// transport / disk I/O). Auth pre-checks still compose via the `#[from]`
/// `Auth` variant; pinned-client setup failures (URL parse, TLS init) bubble
/// via the `#[from]` `Setup` variant.
///
/// Note on `Setup` vs `Auth(Http)`: both can transit an [`HttpError`] at the
/// leaf, but the routing is unambiguous because Rust's `From` is not
/// transitive — `?` on `Result<_, HttpError>` resolves to `Setup` via the
/// direct `From<HttpError> for AttachmentError`, while `?` on
/// `Result<_, AuthError>` resolves to `Auth`. Semantically: `Setup` is for
/// pinned-client construction (`build_pinned_client`), `Auth(Http)` is for
/// HTTP errors that surfaced inside an auth-domain operation
/// (e.g. token refresh).
#[derive(Debug, Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum AttachmentError {
    #[error("{0}")]
    Auth(#[from] AuthError),

    #[error("{0}")]
    Setup(#[from] HttpError),

    #[error("invalid sha256: {0}")]
    InvalidSha256(String),

    /// `size` is the declared `Content-Length`, or without one the bytes
    /// received when the read stopped (a lower bound).
    #[error("attachment too large: {size} bytes (max {max})")]
    TooLarge { size: u64, max: u64 },

    #[error("network error: {0}")]
    Network(String),

    /// The pinned client's total timeout, which includes the body (#464).
    #[error("request timed out after {seconds}s")]
    Timeout { seconds: u64 },

    #[error("server returned HTTP {0}")]
    Http(u16),

    #[error("read body: {0}")]
    Read(String),

    #[error("write {path}: {error}")]
    Write { path: String, error: String },
}

fn validate_sha256(sha256: &str) -> Result<(), AttachmentError> {
    if sha256.len() != 64 || !sha256.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(AttachmentError::InvalidSha256(sha256.to_string()));
    }
    Ok(())
}

impl From<BodyError> for AttachmentError {
    fn from(e: BodyError) -> Self {
        match e {
            BodyError::TooLarge { size } => AttachmentError::TooLarge {
                size,
                max: MAX_ATTACHMENT_BYTES,
            },
            BodyError::Timeout => AttachmentError::Timeout {
                seconds: REQUEST_TIMEOUT_SECS,
            },
            BodyError::Read(msg) => AttachmentError::Read(msg),
        }
    }
}

impl AttachmentError {
    /// A failed `send()`: a timeout says so; anything else keeps its causes.
    fn send_failed(e: reqwest::Error) -> Self {
        if e.is_timeout() {
            AttachmentError::Timeout {
                seconds: REQUEST_TIMEOUT_SECS,
            }
        } else {
            AttachmentError::Network(error_chain(&e))
        }
    }
}

#[derive(Debug, Deserialize, Serialize)]
pub struct DownloadResult {
    pub bytes_written: u64,
    pub path: String,
}

pub async fn download_attachment(
    store: &KeyringStore,
    sha256: &str,
    dest: PathBuf,
) -> Result<DownloadResult, AttachmentError> {
    validate_sha256(sha256)?;
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/attachments/{sha256}");
    let resp = client
        .get(&endpoint)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(AttachmentError::send_failed)?;
    if !resp.status().is_success() {
        return Err(AttachmentError::Http(resp.status().as_u16()));
    }
    let bytes = read_capped(resp, MAX_ATTACHMENT_BYTES).await?;
    let bytes_written = bytes.len() as u64;
    // Off the async runtime: up to 100 MiB of blocking disk IO (#465).
    let target = dest.clone();
    let written = tokio::task::spawn_blocking(move || write_replacing(&target, &bytes))
        .await
        .map_err(|e| e.to_string())
        .and_then(|r| r.map_err(|e| e.to_string()));
    written.map_err(|error| AttachmentError::Write {
        path: dest.display().to_string(),
        error,
    })?;
    Ok(DownloadResult {
        bytes_written,
        path: dest.to_string_lossy().to_string(),
    })
}

#[tauri::command]
pub async fn download_attachment_cmd(
    sha256: String,
    dest: String,
) -> Result<DownloadResult, AttachmentError> {
    let store = KeyringStore::new();
    download_attachment(&store, &sha256, PathBuf::from(dest)).await
}

#[derive(Debug, Serialize)]
pub struct AttachmentBlob {
    pub bytes: Vec<u8>,
    pub content_type: Option<String>,
}

pub async fn fetch_attachment_bytes(
    store: &KeyringStore,
    sha256: &str,
) -> Result<AttachmentBlob, AttachmentError> {
    validate_sha256(sha256)?;
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/attachments/{sha256}");
    let resp = client
        .get(&endpoint)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(AttachmentError::send_failed)?;
    if !resp.status().is_success() {
        return Err(AttachmentError::Http(resp.status().as_u16()));
    }
    let content_type = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());
    let bytes = read_capped(resp, MAX_ATTACHMENT_BYTES).await?;
    Ok(AttachmentBlob {
        bytes,
        content_type,
    })
}

#[tauri::command]
pub async fn fetch_attachment_bytes_cmd(sha256: String) -> Result<AttachmentBlob, AttachmentError> {
    let store = KeyringStore::new();
    fetch_attachment_bytes(&store, &sha256).await
}

#[cfg(test)]
#[path = "attachments_tests.rs"]
mod tests;
