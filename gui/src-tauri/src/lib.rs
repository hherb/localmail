//! Tauri command surface for the localmail GUI client.
//!
//! Sub-plan 1 ships only the `greet` demo command. Subsequent sub-plans add
//! HTTP, keyring, TOFU, and the API surface the Svelte UI calls into.

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
        .invoke_handler(tauri::generate_handler![greet])
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
