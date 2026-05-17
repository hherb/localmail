//! HTTP layer for the localmail GUI client.
//!
//! Sub-plan 2 ships:
//! - `verifier::TofuVerifier`: rustls ServerCertVerifier with Probe / Pinned modes
//! - `client::http_get`, `client::http_post_json`: thin reqwest wrappers (Task 2)
//! - `errors::HttpError`: typed errors that serialize cleanly to the JS side (Task 2)

pub mod verifier;
