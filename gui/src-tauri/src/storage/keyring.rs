//! OS keyring wrapper.
//!
//! Single service ("localmail-gui"), four distinct keys (one per item).
//! Synchronous API.
//!
//! The `KeyringStore` struct delegates to a `KeyringBackend` trait object so that
//! tests can inject a `HashMap`-backed in-process store without touching the OS
//! keychain.  `keyring::mock` has `EntryOnly` persistence — a new `Entry::new()`
//! call always returns a fresh, empty credential — so it cannot serve as a
//! drop-in for tests that create multiple entries across calls.

use anyhow::{Context, Result};
use keyring::Entry;

const SERVICE: &str = "localmail-gui";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Slot {
    ServerUrl,
    Username,
    CertPin,
    BearerToken,
}

impl Slot {
    fn key(self) -> &'static str {
        match self {
            Slot::ServerUrl => "server_url",
            Slot::Username => "username",
            Slot::CertPin => "cert_sha256_pin",
            Slot::BearerToken => "bearer_token",
        }
    }
}

const ALL_SLOTS: [Slot; 4] = [
    Slot::ServerUrl,
    Slot::Username,
    Slot::CertPin,
    Slot::BearerToken,
];

/// Abstraction over the OS keyring so tests can inject a fake backend.
pub trait KeyringBackend: Send + Sync {
    fn put(&self, slot: Slot, value: &str) -> Result<()>;
    fn get(&self, slot: Slot) -> Result<Option<String>>;
    fn delete(&self, slot: Slot) -> Result<()>;
}

/// Production backend: delegates to the OS keyring via the `keyring` crate.
pub struct OsKeyring;

impl KeyringBackend for OsKeyring {
    fn put(&self, slot: Slot, value: &str) -> Result<()> {
        let entry = Entry::new(SERVICE, slot.key())
            .with_context(|| format!("create keyring entry for {:?}", slot))?;
        entry
            .set_password(value)
            .with_context(|| format!("write keyring entry for {:?}", slot))?;
        Ok(())
    }

    fn get(&self, slot: Slot) -> Result<Option<String>> {
        let entry = Entry::new(SERVICE, slot.key())
            .with_context(|| format!("create keyring entry for {:?}", slot))?;
        match entry.get_password() {
            Ok(v) => Ok(Some(v)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(e) => Err(anyhow::anyhow!(e))
                .with_context(|| format!("read keyring entry for {:?}", slot)),
        }
    }

    fn delete(&self, slot: Slot) -> Result<()> {
        let entry = Entry::new(SERVICE, slot.key())
            .with_context(|| format!("create keyring entry for {:?}", slot))?;
        match entry.delete_credential() {
            Ok(()) => Ok(()),
            Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(anyhow::anyhow!(e))
                .with_context(|| format!("delete keyring entry for {:?}", slot)),
        }
    }
}

/// High-level keyring store.  Uses `OsKeyring` by default; tests inject a
/// `MemKeyring` backend via `KeyringStore::with_backend`.
pub struct KeyringStore {
    backend: Box<dyn KeyringBackend>,
}

impl Default for KeyringStore {
    fn default() -> Self {
        Self::new()
    }
}

impl KeyringStore {
    pub fn new() -> Self {
        Self {
            backend: Box::new(OsKeyring),
        }
    }

    #[cfg(test)]
    pub fn with_backend(backend: impl KeyringBackend + 'static) -> Self {
        Self {
            backend: Box::new(backend),
        }
    }

    pub fn put(&self, slot: Slot, value: &str) -> Result<()> {
        self.backend.put(slot, value)
    }

    pub fn get(&self, slot: Slot) -> Result<Option<String>> {
        self.backend.get(slot)
    }

    pub fn delete(&self, slot: Slot) -> Result<()> {
        self.backend.delete(slot)
    }

    pub fn clear_all(&self) -> Result<()> {
        for slot in ALL_SLOTS {
            self.delete(slot)?;
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex;

    /// In-process HashMap backend — no OS keyring involved.
    struct MemKeyring {
        map: Mutex<HashMap<Slot, String>>,
    }

    impl MemKeyring {
        fn new() -> Self {
            Self {
                map: Mutex::new(HashMap::new()),
            }
        }
    }

    impl KeyringBackend for MemKeyring {
        fn put(&self, slot: Slot, value: &str) -> Result<()> {
            self.map.lock().unwrap().insert(slot, value.to_owned());
            Ok(())
        }

        fn get(&self, slot: Slot) -> Result<Option<String>> {
            Ok(self.map.lock().unwrap().get(&slot).cloned())
        }

        fn delete(&self, slot: Slot) -> Result<()> {
            self.map.lock().unwrap().remove(&slot);
            Ok(())
        }
    }

    fn store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[test]
    fn put_get_round_trip() {
        let s = store();
        s.put(Slot::ServerUrl, "https://localhost:8443").unwrap();
        assert_eq!(
            s.get(Slot::ServerUrl).unwrap().as_deref(),
            Some("https://localhost:8443"),
        );
    }

    #[test]
    fn get_missing_returns_none() {
        let s = store();
        assert!(s.get(Slot::Username).unwrap().is_none());
    }

    #[test]
    fn delete_then_get_returns_none() {
        let s = store();
        s.put(Slot::Username, "alice").unwrap();
        assert!(s.get(Slot::Username).unwrap().is_some());
        s.delete(Slot::Username).unwrap();
        assert!(s.get(Slot::Username).unwrap().is_none());
    }

    #[test]
    fn delete_missing_is_idempotent() {
        let s = store();
        s.delete(Slot::BearerToken).unwrap();
    }

    #[test]
    fn clear_all_removes_every_slot() {
        let s = store();
        s.put(Slot::ServerUrl, "u").unwrap();
        s.put(Slot::Username, "n").unwrap();
        s.put(Slot::CertPin, "p").unwrap();
        s.put(Slot::BearerToken, "t").unwrap();
        s.clear_all().unwrap();
        for slot in ALL_SLOTS {
            assert!(
                s.get(slot).unwrap().is_none(),
                "{:?} should be gone",
                slot
            );
        }
    }
}
