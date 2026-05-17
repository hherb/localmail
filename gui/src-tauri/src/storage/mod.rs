//! Persistent storage for connection state.
//!
//! Wraps the OS keyring (macOS Keychain, Windows Credential Manager,
//! Linux Secret Service) via the `keyring` crate. Stores four items keyed
//! under the same service name `"localmail-gui"`:
//!
//! - `server_url` — string
//! - `username`   — string
//! - `cert_sha256_pin` — string (lowercase hex)
//! - `bearer_token`   — string (never returned to the JS side)

pub mod keyring;
