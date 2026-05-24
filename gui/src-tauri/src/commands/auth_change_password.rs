//! POST /v1/auth/change-password — token stays valid; password is replaced.
//!
//! Mirrors the structure of `commands::auth`: a pure async helper that takes
//! a `KeyringStore` + endpoint + credentials so it can be tested with a
//! mockito server, plus a `#[tauri::command]` wrapper that constructs the
//! real OS-keyring-backed store.
//
// Defence-in-depth note on the password strings: `old_password` /
// `new_password` are owned `String`s on the heap for the duration of the
// request and are not actively zeroized on drop. This matches the rest of
// the codebase (login does the same). If we ever pull in `secrecy` /
// `zeroize`, this path is the natural first adopter.

use reqwest::Client;
use serde::Serialize;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_post_json_no_resp};
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
    // Use the shared helper so 401 (wrong old password — UX-critical) and
    // 500 surface as distinct HttpError::HttpStatus { status, body } variants
    // that the Svelte side can branch on, rather than flattening into one
    // opaque string.
    Ok(http_post_json_no_resp(client, &endpoint, &body, Some(token)).await?)
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
    async fn post_change_password_maps_401_to_structured_http_status() {
        // 401 (wrong old password) is the UX-critical case the GUI must
        // distinguish from 500. Verify the structured HttpError::HttpStatus
        // variant is preserved through AuthError so the Svelte side can
        // branch on status without string-matching.
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
            AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, body }) => {
                assert_eq!(status, 401);
                assert_eq!(body, "invalid old password");
            }
            other => panic!("expected AuthError::Http(HttpStatus {{ 401, .. }}), got {other:?}"),
        }
    }
}
