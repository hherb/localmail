//! Tauri command handlers. Each submodule is a logical group; lib.rs::run()
//! registers them all via `tauri::generate_handler!`.

pub mod accounts;
pub mod auth;
pub mod capabilities;
pub mod changes;
pub mod connect;
pub mod messages;
pub mod session;
