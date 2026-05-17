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
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/attachments/{sha256}");
    let resp = client.get(&endpoint).bearer_auth(&token).send().await
        .map_err(|e| AuthError::Io(format!("attachment request: {e}")))?;
    if !resp.status().is_success() {
        return Err(AuthError::Io(format!("HTTP {} on {endpoint}", resp.status())));
    }
    let bytes = resp.bytes().await
        .map_err(|e| AuthError::Io(format!("read body: {e}")))?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn download_without_connection_returns_not_connected() {
        let s = fake_store();
        let err = download_attachment(&s, "deadbeef", PathBuf::from("/tmp/x"))
            .await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn download_without_token_returns_not_logged_in() {
        let s = fake_store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = download_attachment(&s, "x", PathBuf::from("/tmp/x"))
            .await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }
}
