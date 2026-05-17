//! Login / logout / refresh against `/v1/auth/*`.
//!
//! Each command:
//! 1. Reads URL + cert pin from the keyring.
//! 2. Builds a pinned reqwest client.
//! 3. Calls the appropriate endpoint.
//! 4. Persists or clears the bearer token in the keyring.
//!
//! The bearer token is NEVER returned to the JS side — Rust holds it.

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::http::client::{build_pinned_client, http_get_json, http_post_empty, http_post_json};
use crate::http::errors::HttpError;
use crate::storage::keyring::{KeyringStore, Slot};

#[derive(Debug, Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum AuthError {
    #[error("not connected yet — call confirm_trust first")]
    NotConnected,
    #[error("not logged in")]
    NotLoggedIn,
    #[error("{0}")]
    Http(#[from] HttpError),
    #[error("keyring error: {0}")]
    Keyring(String),
}

#[derive(Debug, Serialize)]
pub struct LoginSummary {
    pub username: String,
    pub expires_at: String,
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    token: String,
    expires_at: String,
}

#[derive(Debug, Serialize)]
struct LoginRequest<'a> {
    username: &'a str,
    password: &'a str,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct WhoamiResponse {
    pub username: String,
    pub user_id: String,
}

fn read_connection(store: &KeyringStore) -> Result<(String, String), AuthError> {
    let url = store
        .get(Slot::ServerUrl)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let pin = store
        .get(Slot::CertPin)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    Ok((url, pin))
}

pub async fn login(store: &KeyringStore, username: &str, password: &str) -> Result<LoginSummary, AuthError> {
    let (url, pin) = read_connection(store)?;
    let client = build_pinned_client(&pin)?;
    let body = LoginRequest { username, password };
    let endpoint = format!("{url}v1/auth/login");
    let tok: TokenResponse = http_post_json(&client, &endpoint, &body, None).await?;
    store.put(Slot::Username, username).map_err(|e| AuthError::Keyring(e.to_string()))?;
    store.put(Slot::BearerToken, &tok.token).map_err(|e| AuthError::Keyring(e.to_string()))?;
    Ok(LoginSummary {
        username: username.to_string(),
        expires_at: tok.expires_at,
    })
}

pub async fn logout(store: &KeyringStore) -> Result<(), AuthError> {
    let (url, pin) = read_connection(store)?;
    let token_opt = store.get(Slot::BearerToken).map_err(|e| AuthError::Keyring(e.to_string()))?;

    if let Some(tok) = &token_opt {
        let client = build_pinned_client(&pin)?;
        let endpoint = format!("{url}v1/auth/logout");
        let _ = http_post_empty(&client, &endpoint, Some(tok)).await;
    }
    store.delete(Slot::BearerToken).map_err(|e| AuthError::Keyring(e.to_string()))?;
    store.delete(Slot::Username).map_err(|e| AuthError::Keyring(e.to_string()))?;
    Ok(())
}

pub async fn refresh(store: &KeyringStore) -> Result<LoginSummary, AuthError> {
    let (url, pin) = read_connection(store)?;
    let token = store
        .get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/auth/refresh");
    let tok: TokenResponse = http_post_json(&client, &endpoint, &serde_json::json!({}), Some(&token)).await?;
    store.put(Slot::BearerToken, &tok.token).map_err(|e| AuthError::Keyring(e.to_string()))?;
    let username = store
        .get(Slot::Username)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .unwrap_or_default();
    Ok(LoginSummary {
        username,
        expires_at: tok.expires_at,
    })
}

pub async fn whoami(store: &KeyringStore) -> Result<WhoamiResponse, AuthError> {
    let (url, pin) = read_connection(store)?;
    let token = store
        .get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/auth/whoami");
    let resp: WhoamiResponse = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(resp)
}

// Thin Tauri command wrappers
#[tauri::command]
pub async fn login_cmd(username: String, password: String) -> Result<LoginSummary, AuthError> {
    let store = KeyringStore::new();
    login(&store, &username, &password).await
}

#[tauri::command]
pub async fn logout_cmd() -> Result<(), AuthError> {
    let store = KeyringStore::new();
    logout(&store).await
}

#[tauri::command]
pub async fn refresh_cmd() -> Result<LoginSummary, AuthError> {
    let store = KeyringStore::new();
    refresh(&store).await
}

#[tauri::command]
pub async fn whoami_cmd() -> Result<WhoamiResponse, AuthError> {
    let store = KeyringStore::new();
    whoami(&store).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn login_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = login(&store, "alice", "hunter2").await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn whoami_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = whoami(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    async fn refresh_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = refresh(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    async fn logout_without_token_is_idempotent() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        logout(&store).await.unwrap();
        assert!(store.get(Slot::BearerToken).unwrap().is_none());
    }
}
