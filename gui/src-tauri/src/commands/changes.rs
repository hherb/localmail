//! GET /v1/changes — recent messages across all accounts.
//!
//! Sub-plan 3 uses the no-cursor form to seed the message list with the
//! latest ~200 messages. A `since` cursor for incremental polling lands in
//! Sub-plan 5 (background change polling).

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::{KeyringStore, Slot};

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
    pub next_cursor: String,
}

fn read_connection(store: &KeyringStore) -> Result<(String, String, String), AuthError> {
    let url = store.get(Slot::ServerUrl)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let pin = store.get(Slot::CertPin)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let token = store.get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    Ok((url, pin, token))
}

pub async fn list_recent_messages(store: &KeyringStore) -> Result<ChangesResponse, AuthError> {
    let (url, pin, token) = read_connection(store)?;
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
    use crate::storage::keyring::MemKeyring;

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
}
