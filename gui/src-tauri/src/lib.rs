//! Tauri command surface for the localmail GUI client.
//!
//! Sub-plan 1 ships only the `greet` demo command. Subsequent sub-plans add
//! HTTP, keyring, TOFU, and the API surface the Svelte UI calls into.

pub mod commands;
pub mod http;
pub mod storage;

use serde::Serialize;

#[derive(Serialize)]
pub struct Greeting {
    pub message: String,
    pub source: &'static str,
}

#[tauri::command]
fn greet(name: &str) -> Greeting {
    Greeting {
        message: format!("Hello, {}! (from Rust)", name),
        source: "tauri-cmd",
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // macOS-specific: Tauri's event loop runs inside Objective-C callbacks
    // declared `extern "C"`, which cannot unwind across the FFI boundary.
    // Without this hook, any panic during window/webview setup surfaces
    // only as "panic in a function that cannot unwind" with the real
    // message lost. Print the payload + location, then abort cleanly.
    //
    // The explicit `process::abort()` matters for dev builds (panic = unwind):
    // without it, the runtime would try to unwind through the Obj-C frames and
    // hit the FFI boundary again. In release (panic = "abort" in Cargo.toml)
    // the runtime aborts on its own once the hook returns, so this is a no-op
    // there — kept for parity so both build profiles behave identically.
    std::panic::set_hook(Box::new(|info| {
        let payload = info
            .payload()
            .downcast_ref::<&str>()
            .map(|s| s.to_string())
            .or_else(|| info.payload().downcast_ref::<String>().cloned())
            .unwrap_or_else(|| "<non-string payload>".to_string());
        let loc = info
            .location()
            .map(|l| format!("{}:{}", l.file(), l.line()))
            .unwrap_or_else(|| "<unknown>".to_string());
        eprintln!("\n[localmail-gui PANIC] at {loc}\n  payload: {payload}\n");
        std::process::abort();
    }));

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|_app| {
            // rustls requires exactly one crypto provider per process.
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
            crate::commands::accounts::list_accounts_cmd,
            crate::commands::accounts::list_folders_cmd,
            crate::commands::changes::list_recent_messages_cmd,
            crate::commands::messages::get_message_cmd,
            crate::commands::full_headers::get_message_full_headers_cmd,
            crate::commands::raw_message::get_message_raw_cmd,
            crate::commands::search::run_search_cmd,
            crate::commands::attachments::download_attachment_cmd,
            crate::commands::attachments::fetch_attachment_bytes_cmd,
            crate::commands::version::get_version_cmd,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::greet;

    #[test]
    fn greet_includes_name_and_marker() {
        let out = greet("world");
        assert!(out.message.contains("world"));
        assert!(out.message.contains("(from Rust)"));
        assert_eq!(out.source, "tauri-cmd");
    }
}
