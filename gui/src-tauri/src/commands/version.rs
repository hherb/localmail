//! GET /v1/version (unauthenticated).
//!
//! Surfaced to the JS side as `get_version_cmd` and consumed by VersionGate
//! at startup to enforce major-version compatibility. Unlike every other
//! authenticated endpoint we only need the URL + cert pin from the keyring —
//! no bearer token, since `/v1/version` is the handshake the rest of auth
//! is built on top of.

use reqwest::Client;
use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::session::read_endpoint;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::KeyringStore;

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct VersionInfo {
    pub api_major: u32,
    pub api_minor: u32,
    pub server_version: Option<String>,
    pub build_hash: Option<String>,
    // Added #278/#300. `#[serde(default)]` so a server predating these keys
    // still decodes — the same back-compat the `is_admin` field takes.
    #[serde(default)]
    pub build_source: Option<String>,
    #[serde(default)]
    pub version_source: Option<String>,
}

// Split out so tests can drive a non-TLS reqwest::Client against mockito while
// the real callers still go through build_pinned_client. Mirrors the pattern
// http/client.rs uses for its own tests.
async fn fetch_version(client: &Client, base_url: &str) -> Result<VersionInfo, AuthError> {
    let endpoint = format!("{base_url}v1/version");
    let resp: VersionInfo = http_get_json(client, &endpoint, None).await?;
    Ok(resp)
}

pub async fn get_version(store: &KeyringStore) -> Result<VersionInfo, AuthError> {
    let (url, pin) = read_endpoint(store)?;
    let client = build_pinned_client(&pin)?;
    fetch_version(&client, &url).await
}

#[tauri::command]
pub async fn get_version_cmd() -> Result<VersionInfo, AuthError> {
    let store = KeyringStore::new();
    get_version(&store).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn get_version_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = get_version(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn fetch_version_calls_v1_version_and_decodes_shape() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/v1/version")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(
                r#"{"api_major":1,"api_minor":2,"server_version":"0.7.3","build_hash":"abc123"}"#,
            )
            .create_async()
            .await;

        // mockito serves HTTP; bypass the pinned TLS client for this test.
        let client = Client::new();
        let base = format!("{}/", server.url());
        let got = fetch_version(&client, &base).await.unwrap();

        assert_eq!(
            got,
            VersionInfo {
                api_major: 1,
                api_minor: 2,
                server_version: Some("0.7.3".to_string()),
                build_hash: Some("abc123".to_string()),
                build_source: None,
                version_source: None,
            }
        );
    }

    #[tokio::test]
    async fn fetch_version_tolerates_null_optional_fields() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/v1/version")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"api_major":1,"api_minor":0,"server_version":null,"build_hash":null}"#)
            .create_async()
            .await;

        let client = Client::new();
        let base = format!("{}/", server.url());
        let got = fetch_version(&client, &base).await.unwrap();
        assert_eq!(got.api_major, 1);
        assert_eq!(got.api_minor, 0);
        assert!(got.server_version.is_none());
        assert!(got.build_hash.is_none());
    }
}
