//! Attachment download.
//!
//! GET /v1/attachments/{sha256} → stream bytes to disk, return byte count.
//! Caller (Svelte) provides the destination path obtained from a save dialog.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::build_pinned_client;
use crate::storage::keyring::KeyringStore;

// Hard ceiling on a single attachment payload. The server is trusted (pinned
// TLS, our own deployment), but a misconfigured or compromised response
// without this guard would buffer the entire body into memory — and for
// `fetch_attachment_bytes`, then re-serialize it as Vec<u8> over IPC.
const MAX_ATTACHMENT_BYTES: u64 = 100 * 1024 * 1024;

fn validate_sha256(sha256: &str) -> Result<(), AuthError> {
    if sha256.len() != 64 || !sha256.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(AuthError::Io(format!(
            "invalid sha256 (expected 64 hex chars, got {:?})",
            sha256
        )));
    }
    Ok(())
}

fn check_content_length(resp: &reqwest::Response) -> Result<(), AuthError> {
    if let Some(len) = resp.content_length() {
        if len > MAX_ATTACHMENT_BYTES {
            return Err(AuthError::Io(format!(
                "attachment too large: {} bytes (max {})",
                len, MAX_ATTACHMENT_BYTES
            )));
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
) -> Result<DownloadResult, AuthError> {
    validate_sha256(sha256)?;
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/attachments/{sha256}");
    let resp = client.get(&endpoint).bearer_auth(&token).send().await
        .map_err(|e| AuthError::Io(format!("attachment request: {e}")))?;
    if !resp.status().is_success() {
        return Err(AuthError::Io(format!("HTTP {} on {endpoint}", resp.status())));
    }
    check_content_length(&resp)?;
    let bytes = resp.bytes().await
        .map_err(|e| AuthError::Io(format!("read body: {e}")))?;
    // Post-check in case Content-Length was missing or understated the body.
    if bytes.len() as u64 > MAX_ATTACHMENT_BYTES {
        return Err(AuthError::Io(format!(
            "attachment too large: {} bytes (max {})",
            bytes.len(), MAX_ATTACHMENT_BYTES
        )));
    }
    std::fs::write(&dest, &bytes)
        .map_err(|e| AuthError::Io(format!("write {}: {e}", dest.display())))?;
    Ok(DownloadResult {
        bytes_written: bytes.len() as u64,
        path: dest.to_string_lossy().to_string(),
    })
}

#[tauri::command]
pub async fn download_attachment_cmd(
    sha256: String,
    dest: String,
) -> Result<DownloadResult, AuthError> {
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
) -> Result<AttachmentBlob, AuthError> {
    validate_sha256(sha256)?;
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/attachments/{sha256}");
    let resp = client.get(&endpoint).bearer_auth(&token).send().await
        .map_err(|e| AuthError::Io(format!("attachment request: {e}")))?;
    if !resp.status().is_success() {
        return Err(AuthError::Io(format!("HTTP {} on {endpoint}", resp.status())));
    }
    check_content_length(&resp)?;
    let content_type = resp.headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok()).map(|s| s.to_string());
    let bytes = resp.bytes().await
        .map_err(|e| AuthError::Io(format!("read body: {e}")))?;
    if bytes.len() as u64 > MAX_ATTACHMENT_BYTES {
        return Err(AuthError::Io(format!(
            "attachment too large: {} bytes (max {})",
            bytes.len(), MAX_ATTACHMENT_BYTES
        )));
    }
    Ok(AttachmentBlob { bytes: bytes.to_vec(), content_type })
}

#[tauri::command]
pub async fn fetch_attachment_bytes_cmd(sha256: String) -> Result<AttachmentBlob, AuthError> {
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
    async fn download_without_connection_returns_not_connected() {
        let s = fake_store();
        let err = download_attachment(&s, VALID_SHA, PathBuf::from("/tmp/x"))
            .await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn download_without_token_returns_not_logged_in() {
        let s = fake_store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = download_attachment(&s, VALID_SHA, PathBuf::from("/tmp/x"))
            .await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    async fn fetch_bytes_without_connection_returns_not_connected() {
        let s = fake_store();
        let err = fetch_attachment_bytes(&s, VALID_SHA).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn fetch_bytes_without_token_returns_not_logged_in() {
        let s = fake_store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = fetch_attachment_bytes(&s, VALID_SHA).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[test]
    fn validate_sha256_accepts_64_hex_chars() {
        assert!(validate_sha256(&"a".repeat(64)).is_ok());
        assert!(validate_sha256(&"0123456789abcdefABCDEF".repeat(3)[..64]).is_ok());
    }

    #[test]
    fn validate_sha256_rejects_wrong_length() {
        assert!(validate_sha256("").is_err());
        assert!(validate_sha256(&"a".repeat(63)).is_err());
        assert!(validate_sha256(&"a".repeat(65)).is_err());
    }

    #[test]
    fn validate_sha256_rejects_non_hex() {
        let mut s = "a".repeat(63);
        s.push('!');
        assert!(validate_sha256(&s).is_err());
        s.pop();
        s.push('z');
        assert!(validate_sha256(&s).is_err());
    }

    #[test]
    fn validate_sha256_rejects_path_traversal() {
        // The most obvious abuse — slashes — fails the hex check.
        assert!(validate_sha256("../../etc/passwd").is_err());
        assert!(validate_sha256(&format!("{}{}{}", "a".repeat(30), "/", "a".repeat(33))).is_err());
    }

    #[tokio::test]
    async fn download_rejects_invalid_sha256_before_keyring_read() {
        let s = fake_store();
        let err = download_attachment(&s, "not-a-sha", PathBuf::from("/tmp/x"))
            .await.unwrap_err();
        assert!(matches!(err, AuthError::Io(_)));
    }

    #[tokio::test]
    async fn fetch_bytes_rejects_invalid_sha256_before_keyring_read() {
        let s = fake_store();
        let err = fetch_attachment_bytes(&s, "not-a-sha").await.unwrap_err();
        assert!(matches!(err, AuthError::Io(_)));
    }
}
