//! Shared keyring-reading helpers used by every authenticated command.
//!
//! Two flavours: one for endpoints that only need the URL+pin (probe / login),
//! one for endpoints that also need a bearer token. Centralising these here
//! means a future Slot rename only has to happen in one place.

use crate::commands::auth::AuthError;
use crate::storage::keyring::{KeyringStore, Slot};

pub fn read_endpoint(store: &KeyringStore) -> Result<(String, String), AuthError> {
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

pub fn read_authenticated(store: &KeyringStore) -> Result<(String, String, String), AuthError> {
    let (url, pin) = read_endpoint(store)?;
    let token = store
        .get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    Ok((url, pin, token))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[test]
    fn read_endpoint_without_url_returns_not_connected() {
        let store = fake_store();
        let err = read_endpoint(&store).unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[test]
    fn read_endpoint_without_pin_returns_not_connected() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        let err = read_endpoint(&store).unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[test]
    fn read_authenticated_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = read_authenticated(&store).unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[test]
    fn read_authenticated_returns_full_triple_when_all_present() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        store.put(Slot::BearerToken, "tok").unwrap();
        let (url, pin, token) = read_authenticated(&store).unwrap();
        assert_eq!(url, "https://localhost:8443/");
        assert_eq!(pin, "deadbeef");
        assert_eq!(token, "tok");
    }
}
