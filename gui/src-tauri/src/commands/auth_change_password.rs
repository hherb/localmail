//! POST /v1/auth/change-password — token stays valid; password is replaced.
//!
//! Mirrors the structure of `commands::auth`: a pure async helper that takes
//! a `KeyringStore` + endpoint + credentials so it can be tested with a
//! mockito server, plus a `#[tauri::command]` wrapper that constructs the
//! real OS-keyring-backed store.

use reqwest::Client;
use serde::Serialize;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::build_pinned_client;
use crate::storage::keyring::KeyringStore;

#[derive(Serialize)]
struct ChangePasswordBody<'a> {
    old_password: &'a str,
    new_password: &'a str,
}

// Split out so tests can drive a non-TLS reqwest::Client against mockito while
// the real callers still go through build_pinned_client. Mirrors the pattern
// used by commands::version::fetch_version.
async fn post_change_password(
    client: &Client,
    base_url: &str,
    token: &str,
    old_password: &str,
    new_password: &str,
) -> Result<(), AuthError> {
    let endpoint = format!("{base_url}v1/auth/change-password");
    let body = ChangePasswordBody {
        old_password,
        new_password,
    };
    // Server returns 204 on success; http_post_empty handles both 2xx and 204
    // explicitly, and a JSON body is allowed even though the verb is POST-empty
    // semantically. Use a manual send to attach the body.
    let resp = client
        .post(&endpoint)
        .bearer_auth(token)
        .json(&body)
        .send()
        .await
        .map_err(|e| AuthError::Io(format!("network: {e}")))?;
    let status = resp.status();
    if !status.is_success() && status.as_u16() != 204 {
        let text = resp.text().await.unwrap_or_default();
        return Err(AuthError::Io(format!(
            "server returned {} ({})",
            status, text
        )));
    }
    Ok(())
}

pub async fn change_password(
    store: &KeyringStore,
    old_password: &str,
    new_password: &str,
) -> Result<(), AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    post_change_password(&client, &url, &token, old_password, new_password).await
}

#[tauri::command]
pub async fn change_password_cmd(
    old_password: String,
    new_password: String,
) -> Result<(), AuthError> {
    let store = KeyringStore::new();
    change_password(&store, &old_password, &new_password).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn change_password_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = change_password(&store, "old", "new").await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn change_password_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = change_password(&store, "old", "new").await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    async fn post_change_password_sends_bearer_and_body_then_accepts_204() {
        let mut server = mockito::Server::new_async().await;
        let m = server
            .mock("POST", "/v1/auth/change-password")
            .match_header("authorization", "Bearer tok-xyz")
            .match_header("content-type", "application/json")
            .match_body(mockito::Matcher::JsonString(
                r#"{"old_password":"old-pw","new_password":"new-pw"}"#.to_string(),
            ))
            .with_status(204)
            .create_async()
            .await;

        // mockito serves HTTP; bypass the pinned TLS client for this test.
        let client = Client::new();
        let base = format!("{}/", server.url());
        post_change_password(&client, &base, "tok-xyz", "old-pw", "new-pw")
            .await
            .expect("204 should succeed");
        m.assert_async().await;
    }

    #[tokio::test]
    async fn post_change_password_maps_4xx_to_io_error() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("POST", "/v1/auth/change-password")
            .with_status(401)
            .with_body("invalid old password")
            .create_async()
            .await;
        let client = Client::new();
        let base = format!("{}/", server.url());
        let err = post_change_password(&client, &base, "tok", "bad", "new")
            .await
            .unwrap_err();
        match err {
            AuthError::Io(msg) => {
                assert!(msg.contains("401"), "expected 401 in message, got {msg}");
            }
            other => panic!("expected Io error, got {other:?}"),
        }
    }
}
