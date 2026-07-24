//! Login / logout / refresh against `/v1/auth/*`.
//!
//! Each command:
//! 1. Reads URL + cert pin from the keyring.
//! 2. Builds a pinned reqwest client.
//! 3. Calls the appropriate endpoint.
//! 4. Persists or clears the bearer token in the keyring.
//!
//! The bearer token is NEVER returned to the JS side — Rust holds it.

use reqwest::Client;
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::http::client::{build_pinned_client, http_get_json, http_post_empty, http_post_json};
use crate::http::errors::HttpError;
use crate::storage::keyring::{KeyringStore, Slot};

use super::session::read_endpoint;

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
    // Absent on a serve older than the bearer-admin release; a viewer-only
    // client must still log in rather than fail to decode.
    #[serde(default)]
    pub is_admin: bool,
}

pub async fn login(store: &KeyringStore, username: &str, password: &str) -> Result<LoginSummary, AuthError> {
    let (url, pin) = read_endpoint(store)?;
    let client = build_pinned_client(&pin)?;
    let body = LoginRequest { username, password };
    let endpoint = format!("{url}v1/auth/login");
    let tok: TokenResponse = http_post_json(&client, &endpoint, &body, None).await?;
    // Token first: if this write fails, we abort before mutating Username, so the
    // store does not end up advertising a new identity with no token to back it.
    store.put(Slot::BearerToken, &tok.token).map_err(|e| AuthError::Keyring(e.to_string()))?;
    store.put(Slot::Username, username).map_err(|e| AuthError::Keyring(e.to_string()))?;
    Ok(LoginSummary {
        username: username.to_string(),
        expires_at: tok.expires_at,
    })
}

pub async fn logout(store: &KeyringStore) -> Result<(), AuthError> {
    let (url, pin) = read_endpoint(store)?;
    let token_opt = store.get(Slot::BearerToken).map_err(|e| AuthError::Keyring(e.to_string()))?;

    if let Some(tok) = &token_opt {
        let client = build_pinned_client(&pin)?;
        let endpoint = format!("{url}v1/auth/logout");
        if let Err(e) = http_post_empty(&client, &endpoint, Some(tok)).await {
            eprintln!(
                "[localmail-gui] logout: server-side token invalidation failed ({e}); clearing local keyring anyway"
            );
        }
    }
    store.delete(Slot::BearerToken).map_err(|e| AuthError::Keyring(e.to_string()))?;
    store.delete(Slot::Username).map_err(|e| AuthError::Keyring(e.to_string()))?;
    Ok(())
}

pub async fn refresh(store: &KeyringStore) -> Result<LoginSummary, AuthError> {
    let (url, pin) = read_endpoint(store)?;
    let token = store
        .get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    let username = store
        .get(Slot::Username)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/auth/refresh");
    let tok: TokenResponse = http_post_json(&client, &endpoint, &serde_json::json!({}), Some(&token)).await?;
    store.put(Slot::BearerToken, &tok.token).map_err(|e| AuthError::Keyring(e.to_string()))?;
    Ok(LoginSummary {
        username,
        expires_at: tok.expires_at,
    })
}

// Split out so tests can drive a non-TLS reqwest::Client against mockito while
// the real caller still goes through build_pinned_client.
async fn fetch_whoami(
    client: &Client,
    base_url: &str,
    token: &str,
) -> Result<WhoamiResponse, AuthError> {
    let endpoint = format!("{base_url}v1/auth/whoami");
    Ok(http_get_json(client, &endpoint, Some(token)).await?)
}

pub async fn whoami(store: &KeyringStore) -> Result<WhoamiResponse, AuthError> {
    let (url, pin) = read_endpoint(store)?;
    let token = store
        .get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    let client = build_pinned_client(&pin)?;
    fetch_whoami(&client, &url, &token).await
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
    async fn refresh_without_username_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        store.put(Slot::BearerToken, "stale-token").unwrap();
        let err = refresh(&store).await.unwrap_err();
        assert!(
            matches!(err, AuthError::NotLoggedIn),
            "expected NotLoggedIn for missing username, got {err:?}"
        );
    }

    #[tokio::test]
    async fn fetch_whoami_parses_is_admin_true() {
        let mut server = mockito::Server::new_async().await;
        let m = server
            .mock("GET", "/v1/auth/whoami")
            .match_header("authorization", "Bearer tok-admin")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"username":"root","user_id":"1","is_admin":true}"#)
            .create_async()
            .await;

        let client = Client::new();
        let base = format!("{}/", server.url());
        let me = fetch_whoami(&client, &base, "tok-admin").await.unwrap();
        assert_eq!(me.username, "root");
        assert!(me.is_admin);
        m.assert_async().await;
    }

    #[tokio::test]
    async fn fetch_whoami_defaults_is_admin_false_on_older_server() {
        // A serve predating the bearer-admin release omits is_admin entirely.
        // Decoding must succeed and fall back to false, not fail the login.
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/v1/auth/whoami")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"username":"viewer","user_id":"7"}"#)
            .create_async()
            .await;

        let client = Client::new();
        let base = format!("{}/", server.url());
        let me = fetch_whoami(&client, &base, "tok").await.unwrap();
        assert!(!me.is_admin);
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
