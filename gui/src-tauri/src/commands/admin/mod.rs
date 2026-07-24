//! Admin-mode command handlers. Every call here drives a `/v1/admin/*`
//! JSON endpoint with the stored bearer token; the server gates them on
//! the token user's `is_admin` flag (403 otherwise).

pub mod accounts;
