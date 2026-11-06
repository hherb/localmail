//! HTTP layer for the localmail GUI client.
//!
//! Sub-plan 2 ships:
//! - `verifier::TofuVerifier`: rustls ServerCertVerifier with Probe / Pinned modes
//! - `client`: thin reqwest wrappers (http_get_json, http_post_json, http_post_empty)
//! - `errors::HttpError`: typed errors that serialize cleanly to the JS side
//! - `body::read_capped`: bounded response-body reads for the download commands (#218)

pub mod body;
pub mod client;
pub mod errors;
pub mod verifier;
