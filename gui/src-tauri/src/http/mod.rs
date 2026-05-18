//! HTTP layer for the localmail GUI client.
//!
//! Sub-plan 2 ships:
//! - `verifier::TofuVerifier`: rustls ServerCertVerifier with Probe / Pinned modes
//! - `client`: thin reqwest wrappers (http_get_json, http_post_json, http_post_empty)
//! - `errors::HttpError`: typed errors that serialize cleanly to the JS side

pub mod errors;
pub mod verifier;
pub mod client;
