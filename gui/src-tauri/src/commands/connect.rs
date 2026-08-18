//! Connection setup: probe the server (TLS handshake + /v1/version),
//! show the cert fingerprint to the user, then save URL + pin to keyring.

use serde::{Deserialize, Serialize};
use url::Url;

use crate::http::client::{build_probe_client, http_get_json};
use crate::http::errors::HttpError;
use crate::storage::keyring::{KeyringStore, Slot};

#[derive(Debug, Serialize, Deserialize, PartialEq)]
pub struct ProbeResult {
    pub api_major: u32,
    pub api_minor: u32,
    pub server_version: String,
    pub cert_sha256: String,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct ConnectionInfo {
    pub server_url: String,
    pub cert_sha256_pin: String,
}

#[derive(Debug, Deserialize)]
struct VersionResponse {
    api_major: u32,
    api_minor: u32,
    server_version: String,
}

#[derive(Debug, thiserror::Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum ConnectError {
    #[error("not connected yet")]
    NotConnected,
    #[error("invalid URL: {0}")]
    BadUrl(String),
    #[error("{0}")]
    Http(#[from] HttpError),
    #[error("keyring write failed: {0}")]
    Keyring(String),
}

pub async fn probe_server(url_str: &str) -> Result<ProbeResult, ConnectError> {
    let parsed = Url::parse(url_str).map_err(|e| ConnectError::BadUrl(e.to_string()))?;
    if parsed.scheme() != "https" {
        return Err(ConnectError::BadUrl("scheme must be https".into()));
    }
    let probe = build_probe_client()?;
    let endpoint = format!("{}v1/version", url_with_trailing_slash(&parsed));
    let version: VersionResponse = http_get_json(&probe.client, &endpoint, None).await?;
    let fingerprint = probe
        .verifier
        .captured_fingerprint()
        .ok_or_else(|| HttpError::Network("TLS handshake did not capture certificate".into()))?;
    Ok(ProbeResult {
        api_major: version.api_major,
        api_minor: version.api_minor,
        server_version: version.server_version,
        cert_sha256: fingerprint,
    })
}

pub fn confirm_trust(
    store: &KeyringStore,
    url: &str,
    cert_sha256: &str,
) -> Result<(), ConnectError> {
    let parsed = Url::parse(url).map_err(|e| ConnectError::BadUrl(e.to_string()))?;
    let normalised = url_with_trailing_slash(&parsed);
    store
        .put(Slot::ServerUrl, &normalised)
        .map_err(|e| ConnectError::Keyring(e.to_string()))?;
    store
        .put(Slot::CertPin, &cert_sha256.to_lowercase())
        .map_err(|e| ConnectError::Keyring(e.to_string()))?;
    store
        .delete(Slot::BearerToken)
        .map_err(|e| ConnectError::Keyring(e.to_string()))?;
    Ok(())
}

pub fn connection_info(store: &KeyringStore) -> Result<ConnectionInfo, ConnectError> {
    let server_url = store
        .get(Slot::ServerUrl)
        .map_err(|e| ConnectError::Keyring(e.to_string()))?
        .ok_or(ConnectError::NotConnected)?;
    let cert_sha256_pin = store
        .get(Slot::CertPin)
        .map_err(|e| ConnectError::Keyring(e.to_string()))?
        .ok_or(ConnectError::NotConnected)?;
    Ok(ConnectionInfo {
        server_url,
        cert_sha256_pin,
    })
}

fn url_with_trailing_slash(parsed: &Url) -> String {
    let s = parsed.as_str();
    if s.ends_with('/') {
        s.to_string()
    } else {
        format!("{s}/")
    }
}

// Tauri command thin wrappers — production constructs KeyringStore::new().
#[tauri::command]
pub async fn probe_server_cmd(url: String) -> Result<ProbeResult, ConnectError> {
    probe_server(&url).await
}

#[tauri::command]
pub fn confirm_trust_cmd(url: String, cert_sha256: String) -> Result<(), ConnectError> {
    let store = KeyringStore::new();
    confirm_trust(&store, &url, &cert_sha256)
}

#[tauri::command]
pub fn get_connection_info_cmd() -> Result<ConnectionInfo, ConnectError> {
    connection_info(&KeyringStore::new())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[test]
    fn url_normalisation_adds_trailing_slash() {
        let p = Url::parse("https://localhost:8443").unwrap();
        assert_eq!(url_with_trailing_slash(&p), "https://localhost:8443/");
        let p2 = Url::parse("https://localhost:8443/").unwrap();
        assert_eq!(url_with_trailing_slash(&p2), "https://localhost:8443/");
    }

    #[tokio::test]
    async fn probe_server_rejects_non_https() {
        let err = probe_server("http://example.com").await.unwrap_err();
        match err {
            ConnectError::BadUrl(m) => assert!(m.contains("https")),
            other => panic!("expected BadUrl, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn probe_server_rejects_garbage_url() {
        let err = probe_server("not a url").await.unwrap_err();
        assert!(matches!(err, ConnectError::BadUrl(_)));
    }

    #[test]
    fn confirm_trust_stores_url_and_pin_and_clears_token() {
        let store = fake_store();
        store.put(Slot::BearerToken, "stale").unwrap();

        confirm_trust(&store, "https://localhost:8443", "ABCDEF").unwrap();

        assert_eq!(
            store.get(Slot::ServerUrl).unwrap().as_deref(),
            Some("https://localhost:8443/")
        );
        assert_eq!(store.get(Slot::CertPin).unwrap().as_deref(), Some("abcdef"));
        assert!(
            store.get(Slot::BearerToken).unwrap().is_none(),
            "stale token must be cleared"
        );
    }

    #[test]
    fn confirm_trust_rejects_bad_url() {
        let store = fake_store();
        let err = confirm_trust(&store, "not a url", "abc").unwrap_err();
        assert!(matches!(err, ConnectError::BadUrl(_)));
    }

    #[test]
    fn connection_info_reads_the_saved_endpoint_without_credentials() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://mail.example/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();

        let info = connection_info(&store).unwrap();
        assert_eq!(info.server_url, "https://mail.example/");
        assert_eq!(info.cert_sha256_pin, "deadbeef");
    }

    #[test]
    fn connection_info_reports_not_connected_when_endpoint_is_absent() {
        let err = connection_info(&fake_store()).unwrap_err();
        assert!(matches!(err, ConnectError::NotConnected));
    }
}
