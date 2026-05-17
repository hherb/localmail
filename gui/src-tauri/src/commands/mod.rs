//! Tauri command handlers. Each submodule is a logical group; lib.rs::run()
//! registers them all via `tauri::generate_handler!`.

pub mod connect;
pub mod auth;
