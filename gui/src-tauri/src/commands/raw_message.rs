//! GET /v1/messages/{id}/raw — returns the raw RFC822 bytes as a Vec<u8>.
//!
//! Used by the GUI's "view raw source" path. The body can be megabytes for
//! messages with large inline attachments, so we cap it the same way
//! `attachments.rs` does to avoid an unbounded buffer / IPC payload.
//!
//! Errors surface via the dedicated [`RawMessageError`] enum — like the
//! attachment commands (issue #22), raw-message I/O failures must not
//! masquerade as auth-domain errors. Auth pre-check failures still compose
//! by bubbling through `RawMessageError::Auth(#[from] AuthError)`.

use serde::Serialize;
use thiserror::Error;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::build_pinned_client;
use crate::http::errors::HttpError;
use crate::storage::keyring::KeyringStore;

// Same ceiling as `attachments::MAX_ATTACHMENT_BYTES`: raw RFC822 includes
// any base64-encoded attachments inline, so the bound has to match.
const MAX_RAW_BYTES: u64 = 100 * 1024 * 1024;

/// Errors raised by the raw-message download command.
#[derive(Debug, Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum RawMessageError {
    #[error("{0}")]
    Auth(#[from] AuthError),

    #[error("{0}")]
    Setup(#[from] HttpError),

    #[error("raw message too large: {size} bytes (max {max})")]
    TooLarge { size: u64, max: u64 },

    #[error("network error: {0}")]
    Network(String),

    #[error("server returned HTTP {0}")]
    Http(u16),

    #[error("read body: {0}")]
    Read(String),
}

pub async fn get_message_raw(
    store: &KeyringStore,
    message_id: &str,
) -> Result<Vec<u8>, RawMessageError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let encoded_id =
        ::url::form_urlencoded::byte_serialize(message_id.as_bytes()).collect::<String>();
    let endpoint = format!("{url}v1/messages/{encoded_id}/raw");
    let resp = client
        .get(&endpoint)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| RawMessageError::Network(e.to_string()))?;
    if !resp.status().is_success() {
        return Err(RawMessageError::Http(resp.status().as_u16()));
    }
    if let Some(len) = resp.content_length() {
        if len > MAX_RAW_BYTES {
            return Err(RawMessageError::TooLarge {
                size: len,
                max: MAX_RAW_BYTES,
            });
        }
    }
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| RawMessageError::Read(e.to_string()))?;
    if bytes.len() as u64 > MAX_RAW_BYTES {
        return Err(RawMessageError::TooLarge {
            size: bytes.len() as u64,
            max: MAX_RAW_BYTES,
        });
    }
    Ok(bytes.to_vec())
}

#[tauri::command]
pub async fn get_message_raw_cmd(message_id: String) -> Result<Vec<u8>, RawMessageError> {
    let store = KeyringStore::new();
    get_message_raw(&store, &message_id).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn get_message_raw_without_connection_returns_auth_not_connected() {
        let store = fake_store();
        let err = get_message_raw(&store, "1").await.unwrap_err();
        assert!(matches!(
            err,
            RawMessageError::Auth(AuthError::NotConnected)
        ));
    }

    #[tokio::test]
    async fn get_message_raw_without_token_returns_auth_not_logged_in() {
        let store = fake_store();
        store
            .put(Slot::ServerUrl, "https://localhost:8443/")
            .unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = get_message_raw(&store, "1").await.unwrap_err();
        assert!(matches!(
            err,
            RawMessageError::Auth(AuthError::NotLoggedIn)
        ));
    }

    #[tokio::test]
    async fn get_message_raw_network_failure_maps_to_network_variant() {
        // Unreachable port → reqwest send() fails → typed RawMessageError::Network.
        // Exercises URL building (host:port:scheme passes through) and the
        // typed transport-error mapping without standing up a real TLS server.
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://127.0.0.1:1/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        store.put(Slot::BearerToken, "tok").unwrap();
        let err = get_message_raw(&store, "msg-42").await.unwrap_err();
        assert!(
            matches!(err, RawMessageError::Network(_)),
            "expected RawMessageError::Network, got {err:?}"
        );
    }

    #[test]
    fn raw_message_error_serializes_with_kind_and_detail_tags() {
        // Lock the wire shape so the JS formatError() walker keeps working.
        let too_large = RawMessageError::TooLarge {
            size: 200 * 1024 * 1024,
            max: MAX_RAW_BYTES,
        };
        assert_eq!(
            serde_json::to_value(&too_large).unwrap(),
            serde_json::json!({
                "kind": "TooLarge",
                "detail": {"size": 200 * 1024 * 1024_u64, "max": MAX_RAW_BYTES},
            }),
        );

        let http = RawMessageError::Http(404);
        assert_eq!(
            serde_json::to_value(&http).unwrap(),
            serde_json::json!({"kind": "Http", "detail": 404}),
        );

        let wrapped: RawMessageError = AuthError::NotConnected.into();
        assert_eq!(
            serde_json::to_value(&wrapped).unwrap(),
            serde_json::json!({"kind": "Auth", "detail": {"kind": "NotConnected"}}),
        );
    }
}
