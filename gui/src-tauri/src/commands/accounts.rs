//! Account + folder listing.
//!
//! Two HTTP calls, both authenticated:
//!   GET /v1/accounts                      → list_accounts
//!   GET /v1/accounts/{account_id}/folders → list_folders
//!
//! Both return JSON arrays decoded into typed structs the Svelte side knows.

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::{KeyringStore, Slot};

#[derive(Debug, Deserialize, Serialize)]
pub struct AccountCapabilities {
    pub can_sync: bool,
    pub is_archive_only: bool,
    pub is_shared: bool,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct AccountSummary {
    pub id: String,
    pub name: String,
    pub address: Option<String>,
    pub last_sync_at: Option<String>,
    pub message_count: i64,
    pub capabilities: AccountCapabilities,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct FolderSummary {
    pub id: String,
    pub name: String,
    pub full_path: String,
    pub flags: Option<String>,
    pub last_uid: Option<i64>,
    pub message_count: i64,
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

pub async fn list_accounts(store: &KeyringStore) -> Result<Vec<AccountSummary>, AuthError> {
    let (url, pin, token) = read_connection(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/accounts");
    let accounts: Vec<AccountSummary> = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(accounts)
}

pub async fn list_folders(store: &KeyringStore, account_id: &str) -> Result<Vec<FolderSummary>, AuthError> {
    let (url, pin, token) = read_connection(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/accounts/{account_id}/folders");
    let folders: Vec<FolderSummary> = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(folders)
}

#[tauri::command]
pub async fn list_accounts_cmd() -> Result<Vec<AccountSummary>, AuthError> {
    let store = KeyringStore::new();
    list_accounts(&store).await
}

#[tauri::command]
pub async fn list_folders_cmd(account_id: String) -> Result<Vec<FolderSummary>, AuthError> {
    let store = KeyringStore::new();
    list_folders(&store, &account_id).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn list_accounts_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = list_accounts(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn list_accounts_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = list_accounts(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    async fn list_folders_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = list_folders(&store, "1").await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }
}
