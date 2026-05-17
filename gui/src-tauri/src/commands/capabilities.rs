//! GET /v1/capabilities — small, authenticated.

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::commands::auth::AuthError;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::{KeyringStore, Slot};

#[derive(Debug, Deserialize, Serialize)]
pub struct Capabilities {
    pub search: bool,
    pub attachments: bool,
    pub attachment_text: bool,
    pub threading: bool,
    pub send: bool,
}

#[derive(Debug, Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum CapabilitiesError {
    #[error("{0}")]
    Auth(#[from] AuthError),
}

pub async fn get_capabilities(store: &KeyringStore) -> Result<Capabilities, CapabilitiesError> {
    let url = store
        .get(Slot::ServerUrl)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let pin = store
        .get(Slot::CertPin)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let token = store
        .get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    let client = build_pinned_client(&pin).map_err(AuthError::from)?;
    let endpoint = format!("{url}v1/capabilities");
    let caps: Capabilities = http_get_json(&client, &endpoint, Some(&token)).await.map_err(AuthError::from)?;
    Ok(caps)
}

#[tauri::command]
pub async fn get_capabilities_cmd() -> Result<Capabilities, CapabilitiesError> {
    let store = KeyringStore::new();
    get_capabilities(&store).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn capabilities_without_login_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = get_capabilities(&store).await.unwrap_err();
        match err {
            CapabilitiesError::Auth(AuthError::NotLoggedIn) => (),
            other => panic!("expected NotLoggedIn, got {:?}", other),
        }
    }
}
