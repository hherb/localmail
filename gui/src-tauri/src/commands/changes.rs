//! GET /v1/changes — tail-only polling for recently-arrived messages.
//!
//! Used by `mail.svelte.ts::pollOnce` for the foreground change banner. The
//! `since` cursor advances forward only; the endpoint is **not** a backwards
//! browse / backfill path (#38). For initial load, pagination, and
//! selection-driven refetch, see `browse.rs` (`GET /v1/messages`).

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::KeyringStore;

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageAddress {
    pub address: Option<String>,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageAccount {
    pub id: String,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageSummary {
    pub message_id: String,
    pub subject: Option<String>,
    pub from: MessageAddress,
    pub date: Option<String>,
    pub account: MessageAccount,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ChangesResponse {
    pub new_messages: Vec<MessageSummary>,
    // Server returns null/absent when there's nothing further to page; modelling
    // this as a required String would cause every initial load to fail.
    pub next_cursor: Option<String>,
}

pub async fn list_recent_messages(store: &KeyringStore) -> Result<ChangesResponse, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/changes");
    let resp: ChangesResponse = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(resp)
}

#[tauri::command]
pub async fn list_recent_messages_cmd() -> Result<ChangesResponse, AuthError> {
    let store = KeyringStore::new();
    list_recent_messages(&store).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn list_recent_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = list_recent_messages(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn list_recent_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = list_recent_messages(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[test]
    fn changes_response_deserialises_null_next_cursor() {
        let body = r#"{"new_messages": [], "next_cursor": null}"#;
        let resp: ChangesResponse = serde_json::from_str(body).unwrap();
        assert!(resp.next_cursor.is_none());
    }

    #[test]
    fn changes_response_deserialises_absent_next_cursor() {
        let body = r#"{"new_messages": []}"#;
        let resp: ChangesResponse = serde_json::from_str(body).unwrap();
        assert!(resp.next_cursor.is_none());
    }

    #[test]
    fn changes_response_deserialises_present_next_cursor() {
        let body = r#"{"new_messages": [], "next_cursor": "cur-123"}"#;
        let resp: ChangesResponse = serde_json::from_str(body).unwrap();
        assert_eq!(resp.next_cursor.as_deref(), Some("cur-123"));
    }
}
