//! GET /v1/messages/{id}/raw — returns the raw RFC822 bytes as a Vec<u8>.
//!
//! Used by the GUI's "view raw source" path. The body can be megabytes for
//! messages with large inline attachments, so we cap it the same way
//! `attachments.rs` does to avoid an unbounded buffer / IPC payload.

use serde::Deserialize;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::build_pinned_client;
use crate::storage::keyring::KeyringStore;

// Same ceiling as `attachments::MAX_ATTACHMENT_BYTES`: raw RFC822 includes
// any base64-encoded attachments inline, so the bound has to match.
const MAX_RAW_BYTES: u64 = 100 * 1024 * 1024;

#[derive(Debug, Deserialize)]
pub struct GetMessageRawArgs {
    pub message_id: String,
}

pub async fn get_message_raw(store: &KeyringStore, message_id: &str) -> Result<Vec<u8>, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/messages/{message_id}/raw");
    let resp = client
        .get(&endpoint)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| AuthError::Io(format!("raw request: {e}")))?;
    if !resp.status().is_success() {
        return Err(AuthError::Io(format!("HTTP {} on {endpoint}", resp.status())));
    }
    if let Some(len) = resp.content_length() {
        if len > MAX_RAW_BYTES {
            return Err(AuthError::Io(format!(
                "raw message too large: {} bytes (max {})",
                len, MAX_RAW_BYTES
            )));
        }
    }
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| AuthError::Io(format!("read body: {e}")))?;
    if bytes.len() as u64 > MAX_RAW_BYTES {
        return Err(AuthError::Io(format!(
            "raw message too large: {} bytes (max {})",
            bytes.len(),
            MAX_RAW_BYTES
        )));
    }
    Ok(bytes.to_vec())
}

#[tauri::command]
pub async fn get_message_raw_cmd(args: GetMessageRawArgs) -> Result<Vec<u8>, AuthError> {
    let store = KeyringStore::new();
    get_message_raw(&store, &args.message_id).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn get_message_raw_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = get_message_raw(&store, "1").await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn get_message_raw_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = get_message_raw(&store, "1").await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    async fn get_message_raw_network_failure_maps_to_io_error() {
        // Unreachable port → reqwest send() fails → mapped to AuthError::Io.
        // This exercises the URL building (host:port:scheme passes through)
        // and the error wrapper without standing up a real TLS server.
        let store = fake_store();
        store
            .put(Slot::ServerUrl, "https://127.0.0.1:1/")
            .unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        store.put(Slot::BearerToken, "tok").unwrap();
        let err = get_message_raw(&store, "msg-42").await.unwrap_err();
        match err {
            AuthError::Io(msg) => assert!(
                msg.starts_with("raw request:"),
                "expected 'raw request:' prefix, got {msg}"
            ),
            other => panic!("expected AuthError::Io, got {other:?}"),
        }
    }
}
