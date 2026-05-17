# localmail GUI Client — Sub-plan 2: Connection core

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Rust connection layer (HTTPS via reqwest + rustls with TOFU cert pinning, OS keyring storage) and the Svelte first-run / login / authenticated-placeholder flow. Done when you launch the app, enter a server URL + credentials, confirm a self-signed cert via TOFU, log in, and see a placeholder screen rendering your username + the server's capabilities (with a working logout button).

**Architecture:** Three Rust modules (`http`, `storage`, `commands`) + four Svelte pieces (auth store, router, ConnectScreen, LoginScreen, AuthenticatedShell). The bearer token never crosses the JS boundary — Rust pulls it from the OS keyring on every authenticated request. TOFU pin uses a custom rustls `ServerCertVerifier` that operates in two modes: `Probe` (returns the leaf cert's SHA-256 fingerprint and accepts everything) and `Pinned` (rejects anything whose SHA-256 doesn't match what's stored).

**Tech Stack:** reqwest 0.12 with `rustls-tls`, rustls 0.23, keyring 3.x (keyring-rs), Svelte 5 runes + stores.

**Base branch:** `main` (Sub-plan 1 merged at `68c2229`). The gui/ scaffolding from Sub-plan 1 is in place, including the strict CSP and macOS panic hook.

**Manual prerequisite for end-to-end smoke testing:** A running `localmail serve` on the user's machine (from the merged `worktree-phase2-hybrid-search` branch — server work merged in PR #6). Smoke instructions in Task 10.

**Out of scope** (later sub-plans):
- Real 3-pane main view, account/folder tree, result list → Sub-plan 3
- Search bar, filters, HTML body rendering, attachments → Sub-plan 4
- Branded icons, production bundle config, version-mismatch handling → Sub-plan 5
- Token refresh background timer (a `setTimeout` ahead of expiry) — placeholder uses manual refresh button only

---

## File structure

```
gui/src-tauri/
  Cargo.toml                  # extended with reqwest, rustls, keyring, anyhow, thiserror, base64
  src/
    main.rs                   # unchanged
    lib.rs                    # extended: registers new commands alongside `greet`
    http/
      mod.rs                  # re-exports
      verifier.rs             # TofuVerifier (Probe / Pinned modes)
      client.rs               # http_get, http_post_json, http_post_empty
      errors.rs               # HttpError + serde Display impl
    storage/
      mod.rs
      keyring.rs              # KeyringStore wrapping keyring-rs
    commands/
      mod.rs                  # re-exports
      connect.rs              # probe_server, confirm_trust
      auth.rs                 # login, logout, refresh_token
      capabilities.rs         # get_capabilities

gui/src/
  App.svelte                  # rewritten — just renders <Router />
  lib/
    tauri.ts                  # extended with new command wrappers
    stores/
      auth.ts                 # Svelte 5 store: AuthState + actions
  routes/
    Router.svelte             # 3-way switch on auth state
  screens/
    ConnectScreen.svelte
    LoginScreen.svelte
    AuthenticatedShell.svelte # placeholder showing username + capabilities + logout
```

---

## Task 0: Worktree + Rust dependencies

**Files:**
- Create worktree at: `.claude/worktrees/gui-client-2`
- Modify: `gui/src-tauri/Cargo.toml`

- [ ] **Step 1: Create worktree off main**

```bash
cd /Users/hherb/src/localmail
git fetch --all
git worktree add .claude/worktrees/gui-client-2 -b gui-client-2 main
cd .claude/worktrees/gui-client-2
git log --oneline -1
```

Expected: HEAD is `68c2229 Merge pull request #14 from hherb/gui-client-1` (or whatever the latest tip is — should include the gui-client-1 merge). All subsequent steps run from inside this worktree.

If the worktree already exists or branch is taken, STOP and report BLOCKED.

- [ ] **Step 2: Verify previous sub-plan state survives**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test 2>&1 | tail -5
```

Expected: `1 passed` (the `greet_includes_name_and_marker` test from Sub-plan 1).

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm test 2>&1 | tail -5
```

Expected: `1 passed`. If either fails, STOP — the worktree is in a bad state.

- [ ] **Step 3: Extend `gui/src-tauri/Cargo.toml` `[dependencies]`**

Find the existing `[dependencies]` block (currently has `tauri`, `tauri-plugin-shell`, `serde`, `serde_json`). Add these entries:

```toml
reqwest = { version = "0.12", default-features = false, features = ["rustls-tls-manual-roots", "json", "http2", "charset"] }
rustls = { version = "0.23", default-features = false, features = ["ring", "std"] }
rustls-pki-types = "1"
keyring = { version = "3", features = ["apple-native", "windows-native", "linux-native-async-persistent"] }
anyhow = "1"
thiserror = "1"
base64 = "0.22"
tokio = { version = "1", features = ["rt-multi-thread", "macros"] }
sha2 = "0.10"
url = "2"
```

And add a `[dev-dependencies]` block (or extend if it exists):

```toml
[dev-dependencies]
rustls = { version = "0.23", default-features = false, features = ["ring", "std"] }
rcgen = "0.13"      # generate test certs in unit tests
mockito = "1"        # mock HTTP server in unit tests
serial_test = "3"    # serialize keyring tests (process-global state)
```

**Notes on the feature selection:**

- `reqwest` with `default-features = false` strips its default TLS (`native-tls`). We use `rustls-tls-manual-roots` because TOFU pinning replaces the system trust store entirely — we don't want reqwest installing webpki roots that would short-circuit our custom verifier.
- `rustls`'s `ring` feature picks the BoringSSL-derived crypto backend (vs `aws-lc-rs`). It's the historically stable one for cross-platform builds.
- `keyring`'s three platform features (`apple-native`, `windows-native`, `linux-native-async-persistent`) are gated by `#[cfg]` inside the crate itself, so all three can be listed and only the right one for the build target compiles in.
- `mockito` is used for tests that exercise `http_get`/`http_post_json` against a localhost HTTP (not HTTPS) endpoint — TOFU verifier behaviour is tested separately at the rustls layer with `rcgen`-generated certs.

- [ ] **Step 4: Verify the crate still compiles**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo check 2>&1 | tail -20
```

Expected: `Finished ... profile [unoptimized + debuginfo] target(s)`. First run downloads all new deps (~3-5 minutes).

If you get errors:
- `failed to select a version for the requirement keyring`: the keyring 3.x API changed considerably from 2.x. If 3.x is unavailable for your toolchain or the feature flags conflict, pin to `keyring = "2"` and adapt the keyring wrapper in Task 3 to the 2.x API (constructor is `Entry::new(service, user)` for both — small adjustment).
- `rustls-pki-types`: this crate exists for rustls 0.22+. If using an older rustls, drop the dep and import `CertificateDer` from rustls directly.
- For `rcgen` or `mockito` resolution failures, those are dev-only — pin to whatever the most recent compatible version is (`cargo add` will tell you).

Adapt to whatever cargo accepts and note deviations in the commit message.

- [ ] **Step 5: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src-tauri/Cargo.toml gui/src-tauri/Cargo.lock
git commit -m "chore(gui-client): add reqwest, rustls, keyring, anyhow, base64, sha2 deps"
```

---

## Task 1: `http::verifier` — TOFU certificate verifier (rustls 0.23)

**Files:**
- Create: `gui/src-tauri/src/http/mod.rs`
- Create: `gui/src-tauri/src/http/verifier.rs`

This is the load-bearing Rust task. The verifier replaces rustls' built-in trust-store check entirely with a per-request mode: probe (capture fingerprint, accept) or pinned (compare against stored, reject mismatch).

- [ ] **Step 1: Make the http module**

`gui/src-tauri/src/http/mod.rs`:

```rust
//! HTTP layer for the localmail GUI client.
//!
//! Sub-plan 2 ships:
//! - `verifier::TofuVerifier`: rustls ServerCertVerifier with Probe / Pinned modes
//! - `client::http_get`, `client::http_post_json`: thin reqwest wrappers
//! - `errors::HttpError`: typed errors that serialize cleanly to the JS side

pub mod errors;
pub mod verifier;
pub mod client;
```

- [ ] **Step 2: Write the failing test for `TofuVerifier`**

`gui/src-tauri/src/http/verifier.rs` — start with just the tests, no impl yet:

```rust
//! TOFU certificate verifier.
//!
//! Replaces rustls' built-in chain validation with a per-instance policy:
//!
//! - `TofuMode::Probe`: accept any cert. The verifier records the leaf
//!   certificate's SHA-256 fingerprint as it goes by, exposed via
//!   `TofuVerifier::captured_fingerprint()`. Used during first-run
//!   connection so the user can confirm the pin.
//!
//! - `TofuMode::Pinned(sha256)`: accept ONLY if the leaf cert's SHA-256
//!   matches `sha256`. Anything else returns `TlsError::BadCertificate`.
//!
//! Neither mode performs hostname verification, expiry checking, or chain
//! validation. The pin is what authenticates the server — a TOFU pin is
//! all-or-nothing.

use std::sync::Arc;
use std::sync::Mutex;

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::{DigitallySignedStruct, Error as TlsError, SignatureScheme};
use rustls_pki_types::{CertificateDer, ServerName, UnixTime};

#[derive(Debug, Clone)]
pub enum TofuMode {
    /// Accept any certificate; record the leaf's SHA-256 for later confirmation.
    Probe,
    /// Accept only certificates whose leaf SHA-256 matches `expected_hex`.
    Pinned { expected_hex: String },
}

#[derive(Debug)]
pub struct TofuVerifier {
    mode: TofuMode,
    captured: Mutex<Option<String>>,
}

impl TofuVerifier {
    pub fn new(mode: TofuMode) -> Arc<Self> {
        Arc::new(Self { mode, captured: Mutex::new(None) })
    }

    /// Returns the SHA-256 (lowercase hex) of the leaf cert seen during the
    /// last successful handshake. `None` until `verify_server_cert` has been
    /// called at least once.
    pub fn captured_fingerprint(&self) -> Option<String> {
        self.captured.lock().unwrap().clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rcgen::generate_simple_self_signed;

    fn make_cert_der() -> CertificateDer<'static> {
        let cert = generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        CertificateDer::from(cert.cert.der().to_vec())
    }

    fn make_other_cert_der() -> CertificateDer<'static> {
        let cert = generate_simple_self_signed(vec!["other.example".into()]).unwrap();
        CertificateDer::from(cert.cert.der().to_vec())
    }

    fn sha256_hex(der: &[u8]) -> String {
        use sha2::{Digest, Sha256};
        let mut h = Sha256::new();
        h.update(der);
        let out = h.finalize();
        out.iter().map(|b| format!("{:02x}", b)).collect()
    }

    fn server_name() -> ServerName<'static> {
        ServerName::try_from("localhost").unwrap()
    }

    fn now() -> UnixTime {
        UnixTime::now()
    }

    #[test]
    fn probe_mode_accepts_any_cert_and_captures_fingerprint() {
        let cert = make_cert_der();
        let expected = sha256_hex(cert.as_ref());

        let v = TofuVerifier::new(TofuMode::Probe);
        let result = v.verify_server_cert(&cert, &[], &server_name(), &[], now());
        assert!(result.is_ok(), "Probe should accept any cert, got {:?}", result.err());

        assert_eq!(v.captured_fingerprint().as_deref(), Some(expected.as_str()));
    }

    #[test]
    fn pinned_mode_accepts_matching_fingerprint() {
        let cert = make_cert_der();
        let pin = sha256_hex(cert.as_ref());

        let v = TofuVerifier::new(TofuMode::Pinned { expected_hex: pin });
        let result = v.verify_server_cert(&cert, &[], &server_name(), &[], now());
        assert!(result.is_ok(), "Pinned matching should accept, got {:?}", result.err());
    }

    #[test]
    fn pinned_mode_rejects_non_matching_fingerprint() {
        let cert = make_cert_der();
        let other = make_other_cert_der();
        let pin = sha256_hex(cert.as_ref());

        let v = TofuVerifier::new(TofuMode::Pinned { expected_hex: pin });
        let result = v.verify_server_cert(&other, &[], &server_name(), &[], now());
        assert!(result.is_err(), "Pinned non-matching should reject");
        match result {
            Err(TlsError::InvalidCertificate(_)) => (),
            other => panic!("Expected InvalidCertificate, got {:?}", other),
        }
    }

    #[test]
    fn fingerprint_capture_is_case_insensitive_hex_lowercase() {
        let cert = make_cert_der();
        let v = TofuVerifier::new(TofuMode::Probe);
        v.verify_server_cert(&cert, &[], &server_name(), &[], now()).unwrap();
        let fp = v.captured_fingerprint().unwrap();
        assert!(fp.chars().all(|c| c.is_ascii_hexdigit() && (c.is_numeric() || c.is_ascii_lowercase())));
        assert_eq!(fp.len(), 64); // SHA-256 hex = 64 chars
    }
}
```

- [ ] **Step 3: Run, confirm fail**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib http::verifier 2>&1 | tail -20
```

Expected: compile error — `verify_server_cert` (and all `ServerCertVerifier` trait methods) are not implemented. Good.

- [ ] **Step 4: Implement the trait**

Append to `gui/src-tauri/src/http/verifier.rs` (after the `impl TofuVerifier` block, before `#[cfg(test)]`):

```rust
impl ServerCertVerifier for TofuVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, TlsError> {
        use sha2::{Digest, Sha256};
        let mut h = Sha256::new();
        h.update(end_entity.as_ref());
        let digest = h.finalize();
        let fingerprint: String = digest.iter().map(|b| format!("{:02x}", b)).collect();

        *self.captured.lock().unwrap() = Some(fingerprint.clone());

        match &self.mode {
            TofuMode::Probe => Ok(ServerCertVerified::assertion()),
            TofuMode::Pinned { expected_hex } => {
                if fingerprint.eq_ignore_ascii_case(expected_hex) {
                    Ok(ServerCertVerified::assertion())
                } else {
                    Err(TlsError::InvalidCertificate(
                        rustls::CertificateError::ApplicationVerificationFailure,
                    ))
                }
            }
        }
    }

    fn verify_tls12_signature(
        &self,
        _message: &[u8],
        _cert: &CertificateDer<'_>,
        _dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _message: &[u8],
        _cert: &CertificateDer<'_>,
        _dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        // Mirror rustls's default safe set — we don't actually verify
        // signatures (since chain validation is bypassed) but rustls calls
        // this to negotiate which schemes the peer is told we accept.
        vec![
            SignatureScheme::RSA_PKCS1_SHA256,
            SignatureScheme::RSA_PKCS1_SHA384,
            SignatureScheme::RSA_PKCS1_SHA512,
            SignatureScheme::ECDSA_NISTP256_SHA256,
            SignatureScheme::ECDSA_NISTP384_SHA384,
            SignatureScheme::ECDSA_NISTP521_SHA512,
            SignatureScheme::RSA_PSS_SHA256,
            SignatureScheme::RSA_PSS_SHA384,
            SignatureScheme::RSA_PSS_SHA512,
            SignatureScheme::ED25519,
        ]
    }
}
```

Also add at the top of the file (in the imports block, before `pub enum TofuMode`):

```rust
// sha2 is used via fully qualified path in the trait impl below.
```

Actually no — `sha2::{Digest, Sha256}` is used both in the trait impl AND in the tests. Add a top-of-file `use sha2::{Digest, Sha256};` so it's available, and remove the local `use` from `verify_server_cert`. Then update both the impl and test to use the top-of-file import. Cleaner.

Final top-of-file imports for `verifier.rs`:

```rust
use std::sync::{Arc, Mutex};

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::{DigitallySignedStruct, Error as TlsError, SignatureScheme};
use rustls_pki_types::{CertificateDer, ServerName, UnixTime};
use sha2::{Digest, Sha256};
```

Refactor the `verify_server_cert` body accordingly:

```rust
        let mut h = Sha256::new();
        h.update(end_entity.as_ref());
        let digest = h.finalize();
        let fingerprint: String = digest.iter().map(|b| format!("{:02x}", b)).collect();
```

And the test's `sha256_hex` helper already uses `sha2::{Digest, Sha256}` locally — that's fine, leave it.

- [ ] **Step 5: Run, confirm PASS**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib http::verifier 2>&1 | tail -20
```

Expected: 4 PASSED.

If you get `error[E0277]: the trait bound 'TofuVerifier: ServerCertVerifier' is not satisfied` even after implementing the trait, double-check that the imports match the exact rustls version that cargo resolved. The `ServerCertVerifier` trait moved between rustls 0.21 → 0.22 → 0.23 (from `rustls::client::ServerCertVerifier` to `rustls::client::danger::ServerCertVerifier` in 0.23, and the signatures changed to use `rustls_pki_types`). The code above targets 0.23; if cargo resolved an older or newer version, look at `rustls`'s docs via `cargo doc --open --no-deps -p rustls` and adjust.

- [ ] **Step 6: Wire the http module into lib.rs**

Edit `gui/src-tauri/src/lib.rs`. Above the existing `#[derive(Serialize)] pub struct Greeting` line, add:

```rust
pub mod http;
```

Verify nothing else broke:

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test 2>&1 | grep -E "(test result|FAILED|error\[)" | head -10
```

Expected: all tests pass (1 greet + 4 verifier = 5 total in lib).

- [ ] **Step 7: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src-tauri/src/http/ gui/src-tauri/src/lib.rs
git commit -m "feat(gui-client): http::verifier — TOFU cert verifier with Probe/Pinned modes"
```

---

## Task 2: `http::errors` and `http::client` — typed errors + reqwest wrappers

**Files:**
- Create: `gui/src-tauri/src/http/errors.rs`
- Create: `gui/src-tauri/src/http/client.rs`

- [ ] **Step 1: Write `http/errors.rs` first (no test — pure type definition)**

```rust
//! Typed HTTP errors returned by the http::client helpers and ultimately
//! surfaced to the JS side via serde.

use serde::Serialize;
use thiserror::Error;

/// Returned by all http::client helpers. The display form is the message
/// shown to the user.
#[derive(Debug, Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum HttpError {
    #[error("network error: {0}")]
    Network(String),

    #[error("TLS certificate did not match pinned fingerprint")]
    CertMismatch,

    #[error("server returned HTTP {status}: {body}")]
    HttpStatus { status: u16, body: String },

    #[error("could not parse server response as JSON: {0}")]
    Decode(String),

    #[error("invalid server URL: {0}")]
    BadUrl(String),

    #[error("request timed out after {seconds}s")]
    Timeout { seconds: u64 },
}

impl HttpError {
    pub fn from_reqwest(err: reqwest::Error) -> Self {
        if err.is_timeout() {
            Self::Timeout { seconds: 0 } // reqwest doesn't expose the timeout value
        } else if err.is_decode() {
            Self::Decode(err.to_string())
        } else {
            // Heuristic: rustls TOFU pin rejection surfaces as a TLS error wrapped
            // in reqwest. The TlsError::InvalidCertificate message contains
            // "ApplicationVerificationFailure" because of how our verifier reports it.
            let s = err.to_string();
            if s.contains("ApplicationVerificationFailure") || s.contains("certificate") {
                Self::CertMismatch
            } else {
                Self::Network(s)
            }
        }
    }
}
```

- [ ] **Step 2: Write the failing test for `http::client`**

`gui/src-tauri/src/http/client.rs`:

```rust
//! Thin async helpers around reqwest. All HTTPS calls go through here so the
//! TLS config (TOFU verifier) is set up exactly once per call.
//!
//! Two modes:
//! - `with_probe_verifier`: returns the configured client AND the verifier
//!   handle so the caller can read `captured_fingerprint()` after the request.
//! - `with_pinned_verifier`: enforces the pin; verifier handle not needed.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Client;
use rustls::ClientConfig;
use serde::de::DeserializeOwned;
use serde::Serialize;

use crate::http::errors::HttpError;
use crate::http::verifier::{TofuMode, TofuVerifier};

const REQUEST_TIMEOUT_SECS: u64 = 15;

pub struct ProbeClient {
    pub client: Client,
    pub verifier: Arc<TofuVerifier>,
}

pub fn build_probe_client() -> Result<ProbeClient, HttpError> {
    let verifier = TofuVerifier::new(TofuMode::Probe);
    let client = build_reqwest_with_verifier(verifier.clone())?;
    Ok(ProbeClient { client, verifier })
}

pub fn build_pinned_client(expected_hex: &str) -> Result<Client, HttpError> {
    let verifier = TofuVerifier::new(TofuMode::Pinned {
        expected_hex: expected_hex.to_string(),
    });
    build_reqwest_with_verifier(verifier)
}

fn build_reqwest_with_verifier(verifier: Arc<TofuVerifier>) -> Result<Client, HttpError> {
    let crypto_provider = rustls::crypto::ring::default_provider();
    let tls_config = ClientConfig::builder_with_provider(crypto_provider.into())
        .with_safe_default_protocol_versions()
        .map_err(|e| HttpError::Network(format!("rustls config: {e}")))?
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();

    Client::builder()
        .use_preconfigured_tls(tls_config)
        .timeout(Duration::from_secs(REQUEST_TIMEOUT_SECS))
        .build()
        .map_err(HttpError::from_reqwest)
}

pub async fn http_get_json<T: DeserializeOwned>(
    client: &Client,
    url: &str,
    bearer: Option<&str>,
) -> Result<T, HttpError> {
    let mut req = client.get(url);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req.send().await.map_err(HttpError::from_reqwest)?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(HttpError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }
    resp.json::<T>().await.map_err(HttpError::from_reqwest)
}

pub async fn http_post_json<B: Serialize, T: DeserializeOwned>(
    client: &Client,
    url: &str,
    body: &B,
    bearer: Option<&str>,
) -> Result<T, HttpError> {
    let mut req = client.post(url).json(body);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req.send().await.map_err(HttpError::from_reqwest)?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(HttpError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }
    resp.json::<T>().await.map_err(HttpError::from_reqwest)
}

pub async fn http_post_empty(
    client: &Client,
    url: &str,
    bearer: Option<&str>,
) -> Result<(), HttpError> {
    let mut req = client.post(url);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req.send().await.map_err(HttpError::from_reqwest)?;
    let status = resp.status();
    if !status.is_success() && status.as_u16() != 204 {
        let body = resp.text().await.unwrap_or_default();
        return Err(HttpError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Debug, Deserialize, PartialEq)]
    struct Echo { message: String }

    #[tokio::test]
    async fn http_get_json_decodes_response() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/echo")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"message":"hi"}"#)
            .create_async()
            .await;

        // mockito is HTTP (not HTTPS), so use a plain reqwest::Client here —
        // the TOFU client is for HTTPS endpoints which we test in the verifier task.
        let client = Client::new();
        let url = format!("{}/echo", server.url());
        let got: Echo = http_get_json(&client, &url, None).await.unwrap();
        assert_eq!(got, Echo { message: "hi".into() });
    }

    #[tokio::test]
    async fn http_get_json_maps_4xx_to_http_status() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/forbidden")
            .with_status(403)
            .with_body("nope")
            .create_async()
            .await;
        let client = Client::new();
        let url = format!("{}/forbidden", server.url());
        let err = http_get_json::<Echo>(&client, &url, None).await.unwrap_err();
        match err {
            HttpError::HttpStatus { status, body } => {
                assert_eq!(status, 403);
                assert_eq!(body, "nope");
            }
            other => panic!("expected HttpStatus, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn http_get_json_attaches_bearer_header() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/ping")
            .match_header("authorization", "Bearer abc123")
            .with_status(200)
            .with_body(r#"{"message":"ok"}"#)
            .create_async()
            .await;
        let client = Client::new();
        let url = format!("{}/ping", server.url());
        let got: Echo = http_get_json(&client, &url, Some("abc123")).await.unwrap();
        assert_eq!(got.message, "ok");
    }

    #[tokio::test]
    async fn http_post_empty_accepts_204() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("POST", "/logout")
            .with_status(204)
            .create_async()
            .await;
        let client = Client::new();
        let url = format!("{}/logout", server.url());
        http_post_empty(&client, &url, Some("tok")).await.unwrap();
    }

    #[test]
    fn build_probe_client_returns_usable_client_and_verifier() {
        let p = build_probe_client().expect("probe client builds");
        assert!(p.verifier.captured_fingerprint().is_none());
        // We don't actually fire a request here — that's the integration test
        // in `verifier.rs`. This just confirms the rustls config wires up.
    }
}
```

- [ ] **Step 3: Confirm fail**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib http::client 2>&1 | tail -20
```

Expected: compile errors (the helpers don't exist yet — wait, they do; my step ordering is off. The above test block IS in the same file as the impl. So the test will run alongside the impl. If you ran the test as "fail first," it would fail to compile. Easier just to write impl + tests together for client.rs since the impl is short).

Actually, re-read Step 2: it writes BOTH the impl AND the tests. So Step 3 should be "Run, confirm PASS":

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib http::client 2>&1 | tail -20
```

Expected: 5 PASSED.

If `rustls::crypto::ring::default_provider()` fails to resolve, try `rustls::crypto::ring::default_provider()` directly, OR install the provider globally via `rustls::crypto::ring::default_provider().install_default().ok();` once at program start (Tauri `setup` hook). The exact mechanism depends on rustls 0.23.x patch version.

If reqwest's `use_preconfigured_tls` is missing, ensure `reqwest`'s feature list in Cargo.toml includes `rustls-tls-manual-roots` (from Task 0).

- [ ] **Step 4: Run the full lib test suite to confirm no regression**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib 2>&1 | grep -E "(test result|FAILED)" | head
```

Expected: all green (1 greet + 4 verifier + 5 client = 10).

- [ ] **Step 5: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src-tauri/src/http/
git commit -m "feat(gui-client): http::client + http::errors — reqwest with TOFU TLS"
```

---

## Task 3: `storage::keyring` — OS keyring wrapper

**Files:**
- Create: `gui/src-tauri/src/storage/mod.rs`
- Create: `gui/src-tauri/src/storage/keyring.rs`

- [ ] **Step 1: Make the storage module and wire into lib.rs**

`gui/src-tauri/src/storage/mod.rs`:

```rust
//! Persistent storage for connection state.
//!
//! Wraps the OS keyring (macOS Keychain, Windows Credential Manager,
//! Linux Secret Service) via the `keyring` crate. Stores 4 items keyed
//! under the same service name `"localmail-gui"`:
//!
//! - `server_url` — string
//! - `username`   — string
//! - `cert_sha256_pin` — string (lowercase hex)
//! - `bearer_token`   — string (returned only by login/refresh, never by the JS side)

pub mod keyring;
```

Edit `gui/src-tauri/src/lib.rs` and add (above `pub mod http;`):

```rust
pub mod storage;
```

- [ ] **Step 2: Write the failing tests**

`gui/src-tauri/src/storage/keyring.rs`:

```rust
//! OS keyring wrapper.
//!
//! Single service ("localmail-gui"), four distinct keys (one per item).
//! Synchronous API even though some platforms back this with an async
//! daemon — keyring's async-persistent backend is opt-in.

use anyhow::{Context, Result};
use keyring::Entry;

const SERVICE: &str = "localmail-gui";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
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

pub struct KeyringStore;

impl KeyringStore {
    pub fn put(&self, slot: Slot, value: &str) -> Result<()> {
        let entry = Entry::new(SERVICE, slot.key())
            .with_context(|| format!("create keyring entry for {:?}", slot))?;
        entry.set_password(value)
            .with_context(|| format!("write keyring entry for {:?}", slot))?;
        Ok(())
    }

    pub fn get(&self, slot: Slot) -> Result<Option<String>> {
        let entry = Entry::new(SERVICE, slot.key())
            .with_context(|| format!("create keyring entry for {:?}", slot))?;
        match entry.get_password() {
            Ok(v) => Ok(Some(v)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(e) => Err(anyhow::anyhow!(e)).with_context(|| format!("read keyring entry for {:?}", slot)),
        }
    }

    pub fn delete(&self, slot: Slot) -> Result<()> {
        let entry = Entry::new(SERVICE, slot.key())
            .with_context(|| format!("create keyring entry for {:?}", slot))?;
        match entry.delete_credential() {
            Ok(()) => Ok(()),
            Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(anyhow::anyhow!(e)).with_context(|| format!("delete keyring entry for {:?}", slot)),
        }
    }

    pub fn clear_all(&self) -> Result<()> {
        for slot in [Slot::ServerUrl, Slot::Username, Slot::CertPin, Slot::BearerToken] {
            self.delete(slot)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;

    // keyring 3.x exposes a `mock` module gated by the `mock` feature for
    // testing. We set it as the default keyring at the start of each test.
    // Tests are serialised because the keyring backend is process-global.

    fn set_mock_keyring() {
        keyring::set_default_credential_builder(keyring::mock::default_credential_builder());
    }

    #[test]
    #[serial]
    fn put_get_round_trip() {
        set_mock_keyring();
        let store = KeyringStore;
        store.put(Slot::ServerUrl, "https://localhost:8443").unwrap();
        assert_eq!(
            store.get(Slot::ServerUrl).unwrap().as_deref(),
            Some("https://localhost:8443"),
        );
    }

    #[test]
    #[serial]
    fn get_missing_returns_none() {
        set_mock_keyring();
        let store = KeyringStore;
        // mock backend starts empty per test; this is None before any put
        assert!(store.get(Slot::Username).unwrap().is_none());
    }

    #[test]
    #[serial]
    fn delete_then_get_returns_none() {
        set_mock_keyring();
        let store = KeyringStore;
        store.put(Slot::Username, "alice").unwrap();
        assert!(store.get(Slot::Username).unwrap().is_some());
        store.delete(Slot::Username).unwrap();
        assert!(store.get(Slot::Username).unwrap().is_none());
    }

    #[test]
    #[serial]
    fn delete_missing_is_idempotent() {
        set_mock_keyring();
        let store = KeyringStore;
        store.delete(Slot::BearerToken).unwrap(); // never put — must succeed
    }

    #[test]
    #[serial]
    fn clear_all_removes_every_slot() {
        set_mock_keyring();
        let store = KeyringStore;
        store.put(Slot::ServerUrl, "u").unwrap();
        store.put(Slot::Username, "n").unwrap();
        store.put(Slot::CertPin, "p").unwrap();
        store.put(Slot::BearerToken, "t").unwrap();
        store.clear_all().unwrap();
        for slot in [Slot::ServerUrl, Slot::Username, Slot::CertPin, Slot::BearerToken] {
            assert!(store.get(slot).unwrap().is_none(), "{:?} should be gone", slot);
        }
    }
}
```

If the `mock` feature isn't available in keyring 3.x (the feature flag changed names between versions), use a different strategy: write a tiny `KeyringBackend` trait, default to `keyring::Entry`, and inject a `HashMap`-backed impl in tests. The plan's `KeyringStore` struct will then hold a `Box<dyn KeyringBackend>` instead of using `Entry::new` directly.

Quick check before writing the tests:

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo doc --no-deps -p keyring 2>&1 | grep -i mock | head -5
```

If `mock` appears as a feature, add `keyring = { version = "3", features = [..., "mock"] }` to `[dev-dependencies]`. If not, adopt the trait-injection pattern.

- [ ] **Step 3: Run, confirm PASS**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib storage 2>&1 | tail -15
```

Expected: 5 PASSED.

- [ ] **Step 4: Run full lib test suite — no regression**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib 2>&1 | grep -E "(test result|FAILED)" | head
```

Expected: all green (greet + http::verifier + http::client + storage::keyring).

- [ ] **Step 5: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src-tauri/src/storage/ gui/src-tauri/src/lib.rs gui/src-tauri/Cargo.toml gui/src-tauri/Cargo.lock
git commit -m "feat(gui-client): storage::keyring — OS keyring wrapper with Slot enum"
```

---

## Task 4: `commands::connect` — probe_server + confirm_trust

**Files:**
- Create: `gui/src-tauri/src/commands/mod.rs`
- Create: `gui/src-tauri/src/commands/connect.rs`

- [ ] **Step 1: Make the commands module + wire into lib.rs**

`gui/src-tauri/src/commands/mod.rs`:

```rust
//! Tauri command handlers. Each submodule is a logical group; lib.rs::run()
//! registers them all via `tauri::generate_handler!`.

pub mod connect;
```

Edit `gui/src-tauri/src/lib.rs`. Add `pub mod commands;` near the other module declarations.

- [ ] **Step 2: Write the test (testing the pure helper, not the #[tauri::command] wrapper)**

`gui/src-tauri/src/commands/connect.rs`:

```rust
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

#[derive(Debug, Deserialize)]
struct VersionResponse {
    api_major: u32,
    api_minor: u32,
    server_version: String,
}

#[derive(Debug, thiserror::Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum ConnectError {
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

pub fn confirm_trust(store: &KeyringStore, url: &str, cert_sha256: &str) -> Result<(), ConnectError> {
    let parsed = Url::parse(url).map_err(|e| ConnectError::BadUrl(e.to_string()))?;
    let normalised = url_with_trailing_slash(&parsed);
    store.put(Slot::ServerUrl, &normalised).map_err(|e| ConnectError::Keyring(e.to_string()))?;
    store.put(Slot::CertPin, &cert_sha256.to_lowercase()).map_err(|e| ConnectError::Keyring(e.to_string()))?;
    // Make sure no stale token survives a "reconnect to a different server"
    store.delete(Slot::BearerToken).map_err(|e| ConnectError::Keyring(e.to_string()))?;
    Ok(())
}

fn url_with_trailing_slash(parsed: &Url) -> String {
    let s = parsed.as_str();
    if s.ends_with('/') {
        s.to_string()
    } else {
        format!("{s}/")
    }
}

// Tauri command thin wrappers — register these in lib.rs::run()
#[tauri::command]
pub async fn probe_server_cmd(url: String) -> Result<ProbeResult, ConnectError> {
    probe_server(&url).await
}

#[tauri::command]
pub fn confirm_trust_cmd(url: String, cert_sha256: String) -> Result<(), ConnectError> {
    let store = KeyringStore;
    confirm_trust(&store, &url, &cert_sha256)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;

    fn set_mock_keyring() {
        keyring::set_default_credential_builder(keyring::mock::default_credential_builder());
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
    #[serial]
    fn confirm_trust_stores_url_and_pin_and_clears_token() {
        set_mock_keyring();
        let store = KeyringStore;
        // Pre-populate a stale token
        store.put(Slot::BearerToken, "stale").unwrap();

        confirm_trust(&store, "https://localhost:8443", "ABCDEF").unwrap();

        assert_eq!(store.get(Slot::ServerUrl).unwrap().as_deref(), Some("https://localhost:8443/"));
        assert_eq!(store.get(Slot::CertPin).unwrap().as_deref(), Some("abcdef"));
        assert!(store.get(Slot::BearerToken).unwrap().is_none(), "stale token must be cleared");
    }

    #[test]
    #[serial]
    fn confirm_trust_rejects_bad_url() {
        set_mock_keyring();
        let store = KeyringStore;
        let err = confirm_trust(&store, "not a url", "abc").unwrap_err();
        assert!(matches!(err, ConnectError::BadUrl(_)));
    }
}
```

- [ ] **Step 3: Confirm pass**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib commands::connect 2>&1 | tail -15
```

Expected: 5 PASSED. (No live HTTPS test for `probe_server` happy-path — that's the manual smoke in Task 10. The two `probe_server_*` tests cover URL validation only.)

- [ ] **Step 4: Full lib suite**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib 2>&1 | grep -E "(test result|FAILED)" | head
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src-tauri/src/commands/ gui/src-tauri/src/lib.rs
git commit -m "feat(gui-client): commands::connect — probe_server + confirm_trust"
```

---

## Task 5: `commands::auth` — login, logout, refresh

**Files:**
- Create: `gui/src-tauri/src/commands/auth.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`

- [ ] **Step 1: Add `pub mod auth;` to `commands/mod.rs`**

Edit `gui/src-tauri/src/commands/mod.rs` to:

```rust
//! Tauri command handlers. Each submodule is a logical group; lib.rs::run()
//! registers them all via `tauri::generate_handler!`.

pub mod connect;
pub mod auth;
```

- [ ] **Step 2: Write the auth commands**

`gui/src-tauri/src/commands/auth.rs`:

```rust
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

    // Best-effort: tell the server to revoke. Ignore network failures.
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
    let store = KeyringStore;
    login(&store, &username, &password).await
}

#[tauri::command]
pub async fn logout_cmd() -> Result<(), AuthError> {
    let store = KeyringStore;
    logout(&store).await
}

#[tauri::command]
pub async fn refresh_cmd() -> Result<LoginSummary, AuthError> {
    let store = KeyringStore;
    refresh(&store).await
}

#[tauri::command]
pub async fn whoami_cmd() -> Result<WhoamiResponse, AuthError> {
    let store = KeyringStore;
    whoami(&store).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;

    fn set_mock_keyring() {
        keyring::set_default_credential_builder(keyring::mock::default_credential_builder());
    }

    #[tokio::test]
    #[serial]
    async fn login_without_connection_returns_not_connected() {
        set_mock_keyring();
        let store = KeyringStore;
        let err = login(&store, "alice", "hunter2").await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    #[serial]
    async fn whoami_without_token_returns_not_logged_in() {
        set_mock_keyring();
        let store = KeyringStore;
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = whoami(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    #[serial]
    async fn refresh_without_token_returns_not_logged_in() {
        set_mock_keyring();
        let store = KeyringStore;
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = refresh(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    #[serial]
    async fn logout_without_token_is_idempotent() {
        set_mock_keyring();
        let store = KeyringStore;
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        // No token stored — still succeeds
        logout(&store).await.unwrap();
        assert!(store.get(Slot::BearerToken).unwrap().is_none());
    }
}
```

- [ ] **Step 3: Confirm pass**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib commands::auth 2>&1 | tail -15
```

Expected: 4 PASSED. (Live login/refresh/logout against a real server is the manual smoke in Task 10.)

- [ ] **Step 4: Full lib suite**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib 2>&1 | grep -E "(test result|FAILED)" | head
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src-tauri/src/commands/
git commit -m "feat(gui-client): commands::auth — login/logout/refresh/whoami"
```

---

## Task 6: `commands::capabilities` + register all commands in `lib.rs::run()`

**Files:**
- Create: `gui/src-tauri/src/commands/capabilities.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`
- Modify: `gui/src-tauri/src/lib.rs`

- [ ] **Step 1: Write `commands/capabilities.rs`**

```rust
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
    let store = KeyringStore;
    get_capabilities(&store).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;
    use crate::storage::keyring::Slot;

    fn set_mock_keyring() {
        keyring::set_default_credential_builder(keyring::mock::default_credential_builder());
    }

    #[tokio::test]
    #[serial]
    async fn capabilities_without_login_returns_not_logged_in() {
        set_mock_keyring();
        let store = KeyringStore;
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = get_capabilities(&store).await.unwrap_err();
        match err {
            CapabilitiesError::Auth(AuthError::NotLoggedIn) => (),
            other => panic!("expected NotLoggedIn, got {:?}", other),
        }
    }
}
```

- [ ] **Step 2: Update `commands/mod.rs`**

```rust
//! Tauri command handlers. Each submodule is a logical group; lib.rs::run()
//! registers them all via `tauri::generate_handler!`.

pub mod connect;
pub mod auth;
pub mod capabilities;
```

- [ ] **Step 3: Register all commands in `lib.rs::run()`**

Edit `gui/src-tauri/src/lib.rs`. Replace the `tauri::generate_handler![greet]` line with:

```rust
        .invoke_handler(tauri::generate_handler![
            greet,
            crate::commands::connect::probe_server_cmd,
            crate::commands::connect::confirm_trust_cmd,
            crate::commands::auth::login_cmd,
            crate::commands::auth::logout_cmd,
            crate::commands::auth::refresh_cmd,
            crate::commands::auth::whoami_cmd,
            crate::commands::capabilities::get_capabilities_cmd,
        ])
```

Also, install the rustls crypto provider on app startup. Tauri 2's `setup` hook runs once before the event loop starts. Edit the `tauri::Builder::default()` chain to add:

```rust
        .setup(|_app| {
            // rustls requires exactly one crypto provider per process. Install
            // ring here so http::client::build_*_client can resolve the default
            // provider when building TLS configs.
            rustls::crypto::ring::default_provider()
                .install_default()
                .ok();
            Ok(())
        })
```

Final `lib.rs::run()` should look like:

```rust
pub fn run() {
    std::panic::set_hook(Box::new(|info| {
        // ... existing panic hook unchanged ...
    }));

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|_app| {
            rustls::crypto::ring::default_provider()
                .install_default()
                .ok();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            greet,
            crate::commands::connect::probe_server_cmd,
            crate::commands::connect::confirm_trust_cmd,
            crate::commands::auth::login_cmd,
            crate::commands::auth::logout_cmd,
            crate::commands::auth::refresh_cmd,
            crate::commands::auth::whoami_cmd,
            crate::commands::capabilities::get_capabilities_cmd,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

- [ ] **Step 4: Full lib suite + cargo build**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib 2>&1 | grep -E "(test result|FAILED)" | head
```

Expected: all green (the new capabilities test adds 1 to the count).

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo build 2>&1 | tail -5
```

Expected: clean build.

- [ ] **Step 5: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src-tauri/src/
git commit -m "feat(gui-client): commands::capabilities + register all commands in run()"
```

---

## Task 7: Svelte — `lib/tauri.ts` extension + `lib/stores/auth.ts`

**Files:**
- Modify: `gui/src/lib/tauri.ts`
- Create: `gui/src/lib/stores/auth.ts`

- [ ] **Step 1: Extend `lib/tauri.ts` with all new command wrappers**

Replace the contents of `gui/src/lib/tauri.ts` with:

```ts
/**
 * Thin typed wrappers around Tauri's invoke().
 *
 * Each exported function corresponds to one #[tauri::command] in src-tauri/.
 */
import { invoke } from "@tauri-apps/api/core";

export interface Greeting {
  message: string;
  source: string;
}

export interface ProbeResult {
  api_major: number;
  api_minor: number;
  server_version: string;
  cert_sha256: string;
}

export interface LoginSummary {
  username: string;
  expires_at: string;
}

export interface WhoamiResponse {
  username: string;
  user_id: string;
}

export interface Capabilities {
  search: boolean;
  attachments: boolean;
  attachment_text: boolean;
  threading: boolean;
  send: boolean;
}

export async function greet(name: string): Promise<Greeting> {
  return invoke<Greeting>("greet", { name });
}

export async function probeServer(url: string): Promise<ProbeResult> {
  return invoke<ProbeResult>("probe_server_cmd", { url });
}

export async function confirmTrust(url: string, certSha256: string): Promise<void> {
  return invoke<void>("confirm_trust_cmd", { url, certSha256 });
}

export async function login(username: string, password: string): Promise<LoginSummary> {
  return invoke<LoginSummary>("login_cmd", { username, password });
}

export async function logout(): Promise<void> {
  return invoke<void>("logout_cmd");
}

export async function refresh(): Promise<LoginSummary> {
  return invoke<LoginSummary>("refresh_cmd");
}

export async function whoami(): Promise<WhoamiResponse> {
  return invoke<WhoamiResponse>("whoami_cmd");
}

export async function getCapabilities(): Promise<Capabilities> {
  return invoke<Capabilities>("get_capabilities_cmd");
}
```

- [ ] **Step 2: Write the failing test for the auth store**

`gui/src/lib/stores/auth.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from "vitest";
import { tick } from "svelte";

// Mock the tauri wrappers BEFORE importing the store.
const probeMock = vi.fn();
const confirmTrustMock = vi.fn();
const loginMock = vi.fn();
const logoutMock = vi.fn();
const whoamiMock = vi.fn();
const getCapabilitiesMock = vi.fn();

vi.mock("../tauri", () => ({
  probeServer: probeMock,
  confirmTrust: confirmTrustMock,
  login: loginMock,
  logout: logoutMock,
  whoami: whoamiMock,
  getCapabilities: getCapabilitiesMock,
}));

import { auth, type AuthState } from "./auth";

describe("auth store", () => {
  beforeEach(() => {
    probeMock.mockReset();
    confirmTrustMock.mockReset();
    loginMock.mockReset();
    logoutMock.mockReset();
    whoamiMock.mockReset();
    getCapabilitiesMock.mockReset();
    auth.reset();
  });

  it("starts in 'connecting' state", () => {
    expect(auth.snapshot.phase).toBe("connecting");
  });

  it("refreshState moves to logged_out when whoami throws NotLoggedIn", async () => {
    whoamiMock.mockRejectedValueOnce({ kind: "NotLoggedIn" });
    await auth.refreshState();
    expect(auth.snapshot.phase).toBe("logged_out");
  });

  it("refreshState moves to logged_in when whoami returns a user", async () => {
    whoamiMock.mockResolvedValueOnce({ username: "alice", user_id: "1" });
    getCapabilitiesMock.mockResolvedValueOnce({
      search: true, attachments: true, attachment_text: true,
      threading: false, send: false,
    });
    await auth.refreshState();
    expect(auth.snapshot.phase).toBe("logged_in");
    if (auth.snapshot.phase === "logged_in") {
      expect(auth.snapshot.username).toBe("alice");
      expect(auth.snapshot.capabilities.search).toBe(true);
    }
  });

  it("probe stores result in needs_trust state", async () => {
    probeMock.mockResolvedValueOnce({
      api_major: 1, api_minor: 0,
      server_version: "0.1.0",
      cert_sha256: "abc123",
    });
    await auth.probe("https://localhost:8443");
    expect(auth.snapshot.phase).toBe("needs_trust");
    if (auth.snapshot.phase === "needs_trust") {
      expect(auth.snapshot.certSha256).toBe("abc123");
      expect(auth.snapshot.url).toBe("https://localhost:8443");
    }
  });

  it("confirmTrust calls Rust and moves to logged_out", async () => {
    probeMock.mockResolvedValueOnce({
      api_major: 1, api_minor: 0,
      server_version: "0.1.0",
      cert_sha256: "abc",
    });
    await auth.probe("https://localhost:8443");
    confirmTrustMock.mockResolvedValueOnce(undefined);
    await auth.confirmTrust();
    expect(confirmTrustMock).toHaveBeenCalledWith("https://localhost:8443", "abc");
    expect(auth.snapshot.phase).toBe("logged_out");
  });

  it("login moves to logged_in via whoami + capabilities", async () => {
    loginMock.mockResolvedValueOnce({ username: "alice", expires_at: "2026-12-01T00:00:00Z" });
    whoamiMock.mockResolvedValueOnce({ username: "alice", user_id: "1" });
    getCapabilitiesMock.mockResolvedValueOnce({
      search: true, attachments: true, attachment_text: true,
      threading: false, send: false,
    });
    await auth.login("alice", "hunter2");
    expect(auth.snapshot.phase).toBe("logged_in");
  });

  it("login failure leaves us in logged_out with errorMessage", async () => {
    loginMock.mockRejectedValueOnce({ kind: "Http", detail: "401: invalid" });
    await auth.login("alice", "wrong");
    expect(auth.snapshot.phase).toBe("logged_out");
    if (auth.snapshot.phase === "logged_out") {
      expect(auth.snapshot.errorMessage).toContain("401");
    }
  });

  it("logout clears state to logged_out", async () => {
    // Set up a logged_in starting state
    loginMock.mockResolvedValueOnce({ username: "alice", expires_at: "x" });
    whoamiMock.mockResolvedValueOnce({ username: "alice", user_id: "1" });
    getCapabilitiesMock.mockResolvedValueOnce({
      search: true, attachments: true, attachment_text: true,
      threading: false, send: false,
    });
    await auth.login("alice", "hunter2");
    expect(auth.snapshot.phase).toBe("logged_in");
    logoutMock.mockResolvedValueOnce(undefined);
    await auth.logout();
    expect(logoutMock).toHaveBeenCalled();
    expect(auth.snapshot.phase).toBe("logged_out");
  });
});
```

- [ ] **Step 3: Create `lib/stores/` dir and confirm fail**

```bash
mkdir -p /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src/lib/stores
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm test 2>&1 | tail -15
```

Expected: import error for `./auth` (file doesn't exist yet).

- [ ] **Step 4: Implement the store**

`gui/src/lib/stores/auth.ts`:

```ts
/**
 * Single source of truth for the GUI's auth lifecycle. Implemented as a
 * Svelte 5 rune-backed singleton (not a writable store) — `$state` gives us
 * fine-grained reactivity in components that read `auth.snapshot.*`.
 *
 * State transitions:
 *
 *   initial ─ refreshState ─► logged_out / logged_in
 *      │
 *      ├─ probe ─► needs_trust ─ confirmTrust ─► logged_out
 *      │
 *      └─ (any) ─ login (success) ─► logged_in
 *               ─ login (failure) ─► logged_out + errorMessage
 *               ─ logout ─► logged_out
 */
import {
  confirmTrust as rustConfirmTrust,
  getCapabilities,
  login as rustLogin,
  logout as rustLogout,
  probeServer,
  refresh as rustRefresh,
  whoami,
  type Capabilities,
  type ProbeResult,
} from "../tauri";

export type AuthState =
  | { phase: "connecting" }
  | {
      phase: "needs_trust";
      url: string;
      apiMajor: number;
      apiMinor: number;
      serverVersion: string;
      certSha256: string;
    }
  | { phase: "logged_out"; errorMessage?: string }
  | {
      phase: "logged_in";
      username: string;
      capabilities: Capabilities;
      expiresAt?: string;
    };

class AuthStore {
  #state: AuthState = $state({ phase: "connecting" });

  get snapshot(): AuthState {
    return this.#state;
  }

  reset(): void {
    this.#state = { phase: "connecting" };
  }

  async refreshState(): Promise<void> {
    try {
      const me = await whoami();
      const caps = await getCapabilities();
      this.#state = { phase: "logged_in", username: me.username, capabilities: caps };
    } catch (err: unknown) {
      const kind = (err as { kind?: string } | undefined)?.kind;
      if (kind === "NotConnected") {
        this.#state = { phase: "connecting" };
      } else {
        this.#state = { phase: "logged_out" };
      }
    }
  }

  async probe(url: string): Promise<void> {
    try {
      const res: ProbeResult = await probeServer(url);
      this.#state = {
        phase: "needs_trust",
        url,
        apiMajor: res.api_major,
        apiMinor: res.api_minor,
        serverVersion: res.server_version,
        certSha256: res.cert_sha256,
      };
    } catch (err: unknown) {
      const msg = formatError(err);
      this.#state = { phase: "connecting", ...({} as object) };
      // For the connect screen we surface the error via a separate signal —
      // for now just throw so the screen can show it.
      throw new Error(msg);
    }
  }

  async confirmTrust(): Promise<void> {
    if (this.#state.phase !== "needs_trust") {
      throw new Error("confirmTrust called when not in needs_trust state");
    }
    await rustConfirmTrust(this.#state.url, this.#state.certSha256);
    this.#state = { phase: "logged_out" };
  }

  async login(username: string, password: string): Promise<void> {
    try {
      await rustLogin(username, password);
      await this.refreshState();
    } catch (err: unknown) {
      this.#state = { phase: "logged_out", errorMessage: formatError(err) };
    }
  }

  async logout(): Promise<void> {
    try {
      await rustLogout();
    } finally {
      this.#state = { phase: "logged_out" };
    }
  }

  async refreshToken(): Promise<void> {
    try {
      await rustRefresh();
      await this.refreshState();
    } catch (err: unknown) {
      this.#state = { phase: "logged_out", errorMessage: formatError(err) };
    }
  }
}

function formatError(err: unknown): string {
  if (err && typeof err === "object") {
    const o = err as { kind?: string; detail?: string };
    if (o.kind && o.detail) return `${o.kind}: ${o.detail}`;
    if (o.kind) return o.kind;
  }
  return String(err);
}

export const auth = new AuthStore();
```

- [ ] **Step 5: Confirm pass**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm test 2>&1 | tail -20
```

Expected: all tests pass (1 from `tauri.test.ts` + 8 from `auth.test.ts` = 9). If `$state` runes don't behave inside the class in jsdom, the test environment may need the Svelte compiler plugin — vitest with `@sveltejs/vite-plugin-svelte` should handle it. If it fails with "$state is not defined", check that vite.config.ts's vitest section sees `.ts` files compiled by Svelte (it does, since vite-plugin-svelte registers a transform for runes).

If `$state` is rejected as not allowed in `.ts` files, move the store to `gui/src/lib/stores/auth.svelte.ts` (Svelte 5 supports runes in `.svelte.ts` files explicitly) and update the import in the test to match.

- [ ] **Step 6: Run svelte-check**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm run check 2>&1 | tail -5
```

Expected: 0 errors.

- [ ] **Step 7: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src/lib/
git commit -m "feat(gui-client): tauri.ts + auth store with phase state machine"
```

---

## Task 8: Svelte — `screens/ConnectScreen.svelte` + Router

**Files:**
- Create: `gui/src/screens/ConnectScreen.svelte`
- Create: `gui/src/routes/Router.svelte`
- Modify: `gui/src/App.svelte`

- [ ] **Step 1: Create the screens and routes directories**

```bash
mkdir -p /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src/screens \
         /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src/routes
```

- [ ] **Step 2: Write `screens/ConnectScreen.svelte`**

```svelte
<script lang="ts">
  import { auth } from "../lib/stores/auth";

  let url: string = $state("https://localhost:8443");
  let probing: boolean = $state(false);
  let error: string | null = $state(null);

  async function onProbe(): Promise<void> {
    error = null;
    probing = true;
    try {
      await auth.probe(url);
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      probing = false;
    }
  }

  async function onTrust(): Promise<void> {
    error = null;
    try {
      await auth.confirmTrust();
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  function onBack(): void {
    error = null;
    auth.reset();
  }
</script>

<main class="container">
  <h1>Connect to localmail server</h1>

  {#if auth.snapshot.phase === "connecting"}
    <form
      onsubmit={(e: Event) => {
        e.preventDefault();
        void onProbe();
      }}
    >
      <label for="server-url">Server URL</label>
      <input
        id="server-url"
        bind:value={url}
        placeholder="https://your-server:8443"
        autocomplete="off"
        spellcheck={false}
      />
      <button type="submit" disabled={probing}>
        {probing ? "Probing…" : "Connect"}
      </button>
    </form>
  {:else if auth.snapshot.phase === "needs_trust"}
    <div class="trust">
      <p>Server responded: <code>localmail {auth.snapshot.serverVersion}</code>
         (API v{auth.snapshot.apiMajor}.{auth.snapshot.apiMinor})</p>
      <p>TLS certificate fingerprint (SHA-256):</p>
      <pre class="fp">{auth.snapshot.certSha256}</pre>
      <p class="warn">
        Verify this fingerprint matches your server's certificate. If you trust this
        fingerprint, this client will pin it — any future certificate change will
        require re-trust.
      </p>
      <div class="row">
        <button onclick={onTrust}>Trust this certificate</button>
        <button onclick={onBack} class="secondary">Back</button>
      </div>
    </div>
  {/if}

  {#if error}
    <p class="error">{error}</p>
  {/if}
</main>

<style>
  .container {
    max-width: 640px;
    margin: 64px auto;
    padding: 24px;
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  }
  form {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  label {
    font-size: 12px;
    color: #555;
  }
  .trust {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .fp {
    margin: 0;
    padding: 12px;
    background: #f4f4f4;
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
    word-break: break-all;
    line-height: 1.4;
  }
  .warn {
    margin: 0;
    padding: 10px 12px;
    background: #fff8dc;
    border-left: 3px solid #d4a017;
    color: #5a4500;
    font-size: 12px;
  }
  .row {
    display: flex;
    gap: 8px;
  }
  .secondary {
    background: #f4f4f4;
    color: #555;
    border-color: #ccc;
  }
  .error {
    margin-top: 16px;
    padding: 12px;
    background: #fdecea;
    border-left: 3px solid #c0392b;
    color: #c0392b;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 12px;
  }
</style>
```

- [ ] **Step 3: Write `routes/Router.svelte` (selects screen based on auth state)**

```svelte
<script lang="ts">
  import { onMount } from "svelte";
  import { auth } from "../lib/stores/auth";
  import ConnectScreen from "../screens/ConnectScreen.svelte";

  // Imported lazily — those screens land in Tasks 9 & 10.
  let LoginScreen: typeof import("../screens/LoginScreen.svelte").default | null = $state(null);
  let AuthenticatedShell: typeof import("../screens/AuthenticatedShell.svelte").default | null = $state(null);

  onMount(async () => {
    // Lazy-load to keep the initial Connect-screen render light.
    LoginScreen = (await import("../screens/LoginScreen.svelte")).default;
    AuthenticatedShell = (await import("../screens/AuthenticatedShell.svelte")).default;
    await auth.refreshState();
  });
</script>

{#if auth.snapshot.phase === "connecting" || auth.snapshot.phase === "needs_trust"}
  <ConnectScreen />
{:else if auth.snapshot.phase === "logged_out"}
  {#if LoginScreen}
    <svelte:component this={LoginScreen} />
  {:else}
    <p style="text-align:center; margin-top:64px;">Loading login…</p>
  {/if}
{:else if auth.snapshot.phase === "logged_in"}
  {#if AuthenticatedShell}
    <svelte:component this={AuthenticatedShell} />
  {:else}
    <p style="text-align:center; margin-top:64px;">Loading…</p>
  {/if}
{/if}
```

- [ ] **Step 4: Rewrite `App.svelte` to delegate to Router**

Replace the contents of `gui/src/App.svelte` with:

```svelte
<script lang="ts">
  import Router from "./routes/Router.svelte";
</script>

<Router />
```

- [ ] **Step 5: Confirm no test regression + svelte-check**

The Router references `LoginScreen.svelte` and `AuthenticatedShell.svelte` which don't exist yet. Tasks 9 and 10 create them. For now, this will break the `import()` calls at runtime BUT svelte-check should pass because dynamic imports aren't type-checked against missing files at this layer.

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm run check 2>&1 | tail -5
```

Expected: 0 errors (dynamic import returning unknown is fine).

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm test 2>&1 | tail -5
```

Expected: 9 passed (existing tests still green; we haven't added new test files in this task).

- [ ] **Step 6: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src/screens/ConnectScreen.svelte gui/src/routes/Router.svelte gui/src/App.svelte
git commit -m "feat(gui-client): ConnectScreen + Router (Login/AuthShell lazy)"
```

---

## Task 9: Svelte — `screens/LoginScreen.svelte`

**Files:**
- Create: `gui/src/screens/LoginScreen.svelte`

- [ ] **Step 1: Write LoginScreen**

```svelte
<script lang="ts">
  import { auth } from "../lib/stores/auth";

  let username: string = $state("");
  let password: string = $state("");
  let pending: boolean = $state(false);

  // Surface the error message stashed on the store when login fails.
  let errorMessage: string | null = $derived(
    auth.snapshot.phase === "logged_out" && auth.snapshot.errorMessage
      ? auth.snapshot.errorMessage
      : null
  );

  async function onSubmit(): Promise<void> {
    pending = true;
    try {
      await auth.login(username, password);
    } finally {
      pending = false;
    }
  }

  function onReconnect(): void {
    auth.reset();
  }
</script>

<main class="container">
  <h1>Log in</h1>

  <form
    onsubmit={(e: Event) => {
      e.preventDefault();
      void onSubmit();
    }}
  >
    <label for="username">Username</label>
    <input id="username" bind:value={username} autocomplete="username" />

    <label for="password">Password</label>
    <input
      id="password"
      type="password"
      bind:value={password}
      autocomplete="current-password"
    />

    <button type="submit" disabled={pending || !username || !password}>
      {pending ? "Logging in…" : "Log in"}
    </button>
  </form>

  {#if errorMessage}
    <p class="error">{errorMessage}</p>
  {/if}

  <button class="link" onclick={onReconnect}>Connect to a different server</button>
</main>

<style>
  .container {
    max-width: 400px;
    margin: 64px auto;
    padding: 24px;
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  }
  form {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  label {
    font-size: 12px;
    color: #555;
    margin-top: 4px;
  }
  button[type="submit"] {
    margin-top: 12px;
  }
  .error {
    margin-top: 16px;
    padding: 12px;
    background: #fdecea;
    border-left: 3px solid #c0392b;
    color: #c0392b;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 12px;
  }
  .link {
    margin-top: 24px;
    padding: 4px 0;
    background: none;
    border: none;
    color: #1a4fc7;
    cursor: pointer;
    font-size: 12px;
    text-align: center;
    width: 100%;
  }
  .link:hover {
    text-decoration: underline;
    background: none;
  }
</style>
```

- [ ] **Step 2: Confirm svelte-check and tests still pass**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm run check 2>&1 | tail -5
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm test 2>&1 | tail -5
```

Expected: 0 errors; tests pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src/screens/LoginScreen.svelte
git commit -m "feat(gui-client): LoginScreen"
```

---

## Task 10: Svelte — `screens/AuthenticatedShell.svelte` + manual smoke

**Files:**
- Create: `gui/src/screens/AuthenticatedShell.svelte`
- Modify: `gui/README.md`

- [ ] **Step 1: Write AuthenticatedShell**

```svelte
<script lang="ts">
  import { auth } from "../lib/stores/auth";

  let pending: boolean = $state(false);

  async function onLogout(): Promise<void> {
    pending = true;
    try {
      await auth.logout();
    } finally {
      pending = false;
    }
  }

  async function onRefresh(): Promise<void> {
    pending = true;
    try {
      await auth.refreshToken();
    } finally {
      pending = false;
    }
  }
</script>

{#if auth.snapshot.phase === "logged_in"}
  {@const snap = auth.snapshot}
  <main class="container">
    <header>
      <h1>localmail</h1>
      <div class="user">
        <span class="username">{snap.username}</span>
        <button onclick={onRefresh} disabled={pending} class="secondary">Refresh token</button>
        <button onclick={onLogout} disabled={pending} class="secondary">Log out</button>
      </div>
    </header>

    <section>
      <h2>Server capabilities</h2>
      <ul>
        <li><span class="cap" class:on={snap.capabilities.search}>search</span></li>
        <li><span class="cap" class:on={snap.capabilities.attachments}>attachments</span></li>
        <li><span class="cap" class:on={snap.capabilities.attachment_text}>attachment_text</span></li>
        <li><span class="cap" class:on={snap.capabilities.threading}>threading</span></li>
        <li><span class="cap" class:on={snap.capabilities.send}>send</span></li>
      </ul>
    </section>

    <section class="placeholder">
      <p>Sub-plan 2 acceptance shell — the real 3-pane main view lands in Sub-plan 3.</p>
    </section>
  </main>
{/if}

<style>
  .container {
    max-width: 720px;
    margin: 48px auto;
    padding: 24px;
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    border-bottom: 1px solid #eee;
    padding-bottom: 12px;
    margin-bottom: 24px;
  }
  .user {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .username {
    font-weight: 600;
    color: #1a4fc7;
  }
  .secondary {
    padding: 4px 10px;
    background: #f4f4f4;
    color: #555;
    border-color: #ccc;
    font-size: 12px;
  }
  ul {
    list-style: none;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .cap {
    display: inline-block;
    padding: 4px 10px;
    border-radius: 12px;
    background: #f4f4f4;
    color: #888;
    font-size: 12px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    text-decoration: line-through;
  }
  .cap.on {
    background: #eef9e5;
    color: #2d6a1a;
    text-decoration: none;
  }
  .placeholder {
    margin-top: 32px;
    padding: 16px;
    background: #fafafa;
    border: 1px dashed #ccc;
    border-radius: 4px;
    text-align: center;
    color: #888;
    font-size: 12px;
  }
</style>
```

- [ ] **Step 2: Final test + check pass**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm run check 2>&1 | tail -5
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui && npm test 2>&1 | tail -5
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo test --lib 2>&1 | grep -E "(test result|FAILED)" | head
```

Expected: svelte-check 0 errors; vitest 9 passed; cargo `15+` tests passed across all lib modules.

- [ ] **Step 3: Build the Rust binary so `tauri dev` picks up new commands**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2/gui/src-tauri && cargo build 2>&1 | tail -3
```

Expected: clean build.

- [ ] **Step 4: Update `gui/README.md` with the Sub-plan 2 manual smoke**

Find the existing `## Manual smoke (Sub-plan 1 acceptance)` section in `gui/README.md` and ADD a new section after it (do NOT replace the Sub-plan 1 section — they coexist):

```markdown
## Manual smoke (Sub-plan 2 acceptance)

Requires `localmail serve` running on your machine. Easiest setup:

```bash
# In a separate terminal, from the localmail repo root:
cd .claude/worktrees/phase2-hybrid-search   # or wherever your server checkout lives
unset VIRTUAL_ENV && uv run localmail add-api-user alice         # if alice doesn't exist
unset VIRTUAL_ENV && uv run localmail serve --bind 127.0.0.1     # listens on https://127.0.0.1:8443
```

Then from the gui client worktree:

```bash
cd gui
npm run tauri dev
```

Acceptance steps:

1. App opens to **Connect** screen with `https://localhost:8443` pre-filled.
2. Click "Connect". After ~1s the screen shows the cert SHA-256 fingerprint.
   The fingerprint should be 64 lowercase hex chars.
3. Click "Trust this certificate". App moves to **Login** screen.
4. Enter `alice` / `hunter2` (whatever password you set in step 0 above) and submit.
   App moves to **Authenticated Shell**.
5. Header shows `alice` and capability pills: `search`, `attachments`, `attachment_text`
   light up green; `threading`, `send` are struck-through grey.
6. Click "Refresh token". UI stays on the same screen; no error.
7. Click "Log out". App moves back to Login.
8. Quit the app (Cmd+Q on macOS). Re-launch (`npm run tauri dev` again).
   App should bypass Connect (pin saved) and go straight to Login (token cleared at logout).
9. Log in again. Should land on AuthShell as before.
10. Quit. Re-launch. Should now go straight to AuthShell (token still valid).

If any step fails, capture the offending console output (DevTools → Console) AND the
output of `npm run tauri dev` from the terminal, then report.

### Inspecting the keyring

After successful login, on macOS:

```bash
security find-generic-password -s localmail-gui -a server_url -w
security find-generic-password -s localmail-gui -a username -w
security find-generic-password -s localmail-gui -a cert_sha256_pin -w
security find-generic-password -s localmail-gui -a bearer_token -w
```

These should show your stored values. After logout, only `server_url`, `cert_sha256_pin`
should remain — `username` and `bearer_token` cleared.
```

- [ ] **Step 5: Commit**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-2
git add gui/src/screens/AuthenticatedShell.svelte gui/README.md
git commit -m "feat(gui-client): AuthenticatedShell + Sub-plan 2 manual smoke docs"
```

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-05-17-localmail-gui-design.md`):

| Spec section | Sub-plan 2 task |
|---|---|
| HTTPS with TOFU pinning | Tasks 1–2 |
| OS keyring for URL/username/token/pin | Task 3 |
| First-run / connect screen with TOFU prompt | Tasks 4 + 8 |
| Login screen | Tasks 5 + 9 |
| Silent token refresh primitive | Task 5 (`refresh_cmd`) — UI for automatic background refresh deferred to a later sub-plan |
| `/v1/capabilities` consumption | Tasks 6 + 10 |
| Bearer never crosses JS/Rust boundary | Enforced by Tasks 5, 6 (token only flows via keyring) |
| Logout revokes server-side + clears keyring | Task 5 |
| Strict CSP preserved | No new `connect-src` needed — Tauri's invoke channel is already allowed; no `<script src>` additions |

**Deferred per spec** (explicitly out of scope for this sub-plan):
- Auto-refresh timer that fires when token expiry < 7 days → Sub-plan 4 (Polish)
- Layout-A 3-pane shell → Sub-plan 3
- Search + reading pane → Sub-plan 4
- Branded icons + bundle config → Sub-plan 5

**Placeholder scan:** None. Every step has concrete code or commands. The two intentional "deferred" notes (auto-refresh, layout shell) are explicit and not "TODO" markers.

**Type/name consistency:**
- Rust `LoginSummary { username, expires_at }` matches TS `LoginSummary { username, expires_at }`.
- Rust `Capabilities` field set `{search, attachments, attachment_text, threading, send}` matches TS interface.
- Rust `ProbeResult { api_major, api_minor, server_version, cert_sha256 }` matches TS interface.
- Tauri command names: `probe_server_cmd`, `confirm_trust_cmd`, `login_cmd`, `logout_cmd`, `refresh_cmd`, `whoami_cmd`, `get_capabilities_cmd` — TS `invoke()` strings match exactly.
- Keyring `Slot` enum values match the human-readable strings used in the macOS `security find-generic-password` examples in the README.

**Known fragile area:** the rustls 0.23 custom certificate verifier API is the most version-sensitive piece. Task 1 includes a fallback note pointing at `cargo doc --open -p rustls` if the trait signatures don't compile against the actually-installed rustls version.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-17-localmail-gui-client-2-connection.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between, fast iteration. 11 tasks (0-10), comparable size to Sub-plans 1.

**2. Inline Execution** — execute in this session via `executing-plans`, batch with checkpoints.

**Which approach?** (Either way, the first action will be Task 0: creating the worktree off `main`.)
