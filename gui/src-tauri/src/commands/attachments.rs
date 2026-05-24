//! Attachment download.
//!
//! GET /v1/attachments/{sha256} → stream bytes to disk, return byte count.
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
use crate::http::client::build_pinned_client;
use crate::http::errors::HttpError;
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
#[derive(Debug, Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum AttachmentError {
    #[error("{0}")]
    Auth(#[from] AuthError),

    #[error("{0}")]
    Setup(#[from] HttpError),

    #[error("invalid sha256: {0}")]
    InvalidSha256(String),

    #[error("attachment too large: {size} bytes (max {max})")]
    TooLarge { size: u64, max: u64 },

    #[error("network error: {0}")]
    Network(String),

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

fn check_content_length(resp: &reqwest::Response) -> Result<(), AttachmentError> {
    if let Some(len) = resp.content_length() {
        if len > MAX_ATTACHMENT_BYTES {
            return Err(AttachmentError::TooLarge {
                size: len,
                max: MAX_ATTACHMENT_BYTES,
            });
        }
    }
    Ok(())
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
        .map_err(|e| AttachmentError::Network(e.to_string()))?;
    if !resp.status().is_success() {
        return Err(AttachmentError::Http(resp.status().as_u16()));
    }
    check_content_length(&resp)?;
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| AttachmentError::Read(e.to_string()))?;
    // Post-check in case Content-Length was missing or understated the body.
    if bytes.len() as u64 > MAX_ATTACHMENT_BYTES {
        return Err(AttachmentError::TooLarge {
            size: bytes.len() as u64,
            max: MAX_ATTACHMENT_BYTES,
        });
    }
    std::fs::write(&dest, &bytes).map_err(|e| AttachmentError::Write {
        path: dest.display().to_string(),
        error: e.to_string(),
    })?;
    Ok(DownloadResult {
        bytes_written: bytes.len() as u64,
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
        .map_err(|e| AttachmentError::Network(e.to_string()))?;
    if !resp.status().is_success() {
        return Err(AttachmentError::Http(resp.status().as_u16()));
    }
    check_content_length(&resp)?;
    let content_type = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| AttachmentError::Read(e.to_string()))?;
    if bytes.len() as u64 > MAX_ATTACHMENT_BYTES {
        return Err(AttachmentError::TooLarge {
            size: bytes.len() as u64,
            max: MAX_ATTACHMENT_BYTES,
        });
    }
    Ok(AttachmentBlob {
        bytes: bytes.to_vec(),
        content_type,
    })
}

#[tauri::command]
pub async fn fetch_attachment_bytes_cmd(sha256: String) -> Result<AttachmentBlob, AttachmentError> {
    let store = KeyringStore::new();
    fetch_attachment_bytes(&store, &sha256).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    // 64 lowercase 'a' — passes validate_sha256.
    const VALID_SHA: &str =
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn download_without_connection_returns_auth_not_connected() {
        let s = fake_store();
        let err = download_attachment(&s, VALID_SHA, PathBuf::from("/tmp/x"))
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            AttachmentError::Auth(AuthError::NotConnected)
        ));
    }

    #[tokio::test]
    async fn download_without_token_returns_auth_not_logged_in() {
        let s = fake_store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = download_attachment(&s, VALID_SHA, PathBuf::from("/tmp/x"))
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            AttachmentError::Auth(AuthError::NotLoggedIn)
        ));
    }

    #[tokio::test]
    async fn fetch_bytes_without_connection_returns_auth_not_connected() {
        let s = fake_store();
        let err = fetch_attachment_bytes(&s, VALID_SHA).await.unwrap_err();
        assert!(matches!(
            err,
            AttachmentError::Auth(AuthError::NotConnected)
        ));
    }

    #[tokio::test]
    async fn fetch_bytes_without_token_returns_auth_not_logged_in() {
        let s = fake_store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = fetch_attachment_bytes(&s, VALID_SHA).await.unwrap_err();
        assert!(matches!(
            err,
            AttachmentError::Auth(AuthError::NotLoggedIn)
        ));
    }

    #[test]
    fn validate_sha256_accepts_64_hex_chars() {
        assert!(validate_sha256(&"a".repeat(64)).is_ok());
        assert!(validate_sha256(&"0123456789abcdefABCDEF".repeat(3)[..64]).is_ok());
    }

    #[test]
    fn validate_sha256_rejects_wrong_length() {
        assert!(matches!(
            validate_sha256(""),
            Err(AttachmentError::InvalidSha256(_))
        ));
        assert!(matches!(
            validate_sha256(&"a".repeat(63)),
            Err(AttachmentError::InvalidSha256(_))
        ));
        assert!(matches!(
            validate_sha256(&"a".repeat(65)),
            Err(AttachmentError::InvalidSha256(_))
        ));
    }

    #[test]
    fn validate_sha256_rejects_non_hex() {
        let mut s = "a".repeat(63);
        s.push('!');
        assert!(matches!(
            validate_sha256(&s),
            Err(AttachmentError::InvalidSha256(_))
        ));
        s.pop();
        s.push('z');
        assert!(matches!(
            validate_sha256(&s),
            Err(AttachmentError::InvalidSha256(_))
        ));
    }

    #[test]
    fn validate_sha256_rejects_path_traversal() {
        // The most obvious abuse — slashes — fails the hex check.
        assert!(matches!(
            validate_sha256("../../etc/passwd"),
            Err(AttachmentError::InvalidSha256(_))
        ));
        assert!(matches!(
            validate_sha256(&format!("{}{}{}", "a".repeat(30), "/", "a".repeat(33))),
            Err(AttachmentError::InvalidSha256(_))
        ));
    }

    #[test]
    fn invalid_sha256_carries_the_offending_input() {
        // The diagnostic is the only signal a JS consumer or developer has —
        // make sure the bad value travels through so it shows up in formatError().
        let err = validate_sha256("oops").unwrap_err();
        match err {
            AttachmentError::InvalidSha256(s) => assert_eq!(s, "oops"),
            other => panic!("expected InvalidSha256, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn download_rejects_invalid_sha256_before_keyring_read() {
        let s = fake_store();
        let err = download_attachment(&s, "not-a-sha", PathBuf::from("/tmp/x"))
            .await
            .unwrap_err();
        assert!(matches!(err, AttachmentError::InvalidSha256(_)));
    }

    #[tokio::test]
    async fn fetch_bytes_rejects_invalid_sha256_before_keyring_read() {
        let s = fake_store();
        let err = fetch_attachment_bytes(&s, "not-a-sha").await.unwrap_err();
        assert!(matches!(err, AttachmentError::InvalidSha256(_)));
    }

    #[test]
    fn attachment_error_serializes_with_kind_and_detail_tags() {
        // The JS side's formatError() walks {kind, detail} chains. Lock the
        // wire shape in for each variant so a future refactor of the enum
        // can't silently change what the UI sees.
        let invalid = AttachmentError::InvalidSha256("oops".into());
        assert_eq!(
            serde_json::to_value(&invalid).unwrap(),
            serde_json::json!({"kind": "InvalidSha256", "detail": "oops"}),
        );

        let too_large = AttachmentError::TooLarge {
            size: 200 * 1024 * 1024,
            max: MAX_ATTACHMENT_BYTES,
        };
        assert_eq!(
            serde_json::to_value(&too_large).unwrap(),
            serde_json::json!({
                "kind": "TooLarge",
                "detail": {"size": 200 * 1024 * 1024_u64, "max": MAX_ATTACHMENT_BYTES},
            }),
        );

        let http = AttachmentError::Http(403);
        assert_eq!(
            serde_json::to_value(&http).unwrap(),
            serde_json::json!({"kind": "Http", "detail": 403}),
        );

        let network = AttachmentError::Network("connection refused".into());
        assert_eq!(
            serde_json::to_value(&network).unwrap(),
            serde_json::json!({"kind": "Network", "detail": "connection refused"}),
        );

        let read = AttachmentError::Read("stream closed".into());
        assert_eq!(
            serde_json::to_value(&read).unwrap(),
            serde_json::json!({"kind": "Read", "detail": "stream closed"}),
        );

        let write = AttachmentError::Write {
            path: "/tmp/x".into(),
            error: "permission denied".into(),
        };
        assert_eq!(
            serde_json::to_value(&write).unwrap(),
            serde_json::json!({
                "kind": "Write",
                "detail": {"path": "/tmp/x", "error": "permission denied"},
            }),
        );
    }

    #[test]
    fn auth_pre_check_wraps_via_from_authentication() {
        // The wire shape for an auth pre-check failure must remain a nested
        // {kind: "Auth", detail: {kind: "NotConnected"}} chain so the
        // existing formatError() walker handles it without change.
        let wrapped: AttachmentError = AuthError::NotConnected.into();
        assert_eq!(
            serde_json::to_value(&wrapped).unwrap(),
            serde_json::json!({"kind": "Auth", "detail": {"kind": "NotConnected"}}),
        );
    }
}
