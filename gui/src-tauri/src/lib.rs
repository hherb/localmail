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
