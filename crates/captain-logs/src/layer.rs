//! Sends `tracing` events (the app's own, the script runtime's, and `log` records from libraries) to app.log.

use std::fmt::Write as _;
use tracing::field::{Field, Visit};
use tracing::{Event, Level, Subscriber};
use tracing_subscriber::filter::Targets;
use tracing_subscriber::layer::Context;
use tracing_subscriber::registry::LookupSpan;
use tracing_subscriber::Layer;

/// A short word for where an event came from: `captain_app::window::titlebar` is `window`, the script runtime is
/// `mods`, a mod's own console output is `mod-script`, anything else is its crate.
pub fn short_source(target: &str) -> &str {
    let mut parts = target.split("::");
    let first = parts.next().unwrap_or("");
    match (first, parts.next()) {
        ("captain_app", None) => "app",
        ("captain_app" | "captain", Some(module)) => module,
        ("gpui_shell", Some("script")) => "mod-script",
        ("gpui_shell", _) => "mods",
        (other, _) => other,
    }
}

/// The mod a script error belongs to, read from the first file its stack names (`.../mods/<name>/main.js`).
pub fn mod_name(message: &str) -> Option<&str> {
    ["/mods/", "/frame/", "/views/"].iter().filter_map(|marker| {
        let at = message.find(marker)?;
        let rest = &message[at + marker.len()..];
        let name = &rest[..rest.find('/')?];
        (!name.is_empty() && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')).then_some((at, name))
    }).min_by_key(|(at, _)| *at).map(|(_, name)| name)
}

#[derive(Default)]
struct Fields {
    message: String,
    rest: String,
    log_target: Option<String>,
}

impl Visit for Fields {
    fn record_str(&mut self, field: &Field, value: &str) {
        match field.name() {
            "message" => self.message.push_str(value),
            "log.target" => self.log_target = Some(value.to_owned()),
            name if name.starts_with("log.") => {}
            name => {
                let _ = write!(self.rest, " {name}={value}");
            }
        }
    }

    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        match field.name() {
            "message" => {
                let _ = write!(self.message, "{value:?}");
            }
            name if name.starts_with("log.") => {}
            name => {
                let _ = write!(self.rest, " {name}={value:?}");
            }
        }
    }
}

struct FileLayer;

impl<S: Subscriber> Layer<S> for FileLayer {
    fn on_event(&self, event: &Event<'_>, _: Context<'_, S>) {
        let mut fields = Fields::default();
        event.record(&mut fields);
        let meta = event.metadata();
        let level = match *meta.level() {
            Level::ERROR => "ERROR",
            Level::WARN => "WARN",
            Level::INFO => "INFO",
            _ => "DEBUG",
        };
        let target = fields.log_target.as_deref().unwrap_or(meta.target());
        let source = short_source(target);
        let mut message = fields.message;
        message.push_str(&fields.rest);
        // A script error names its mod first, so a search for the mod finds it.
        if source.starts_with("mod")
            && let Some(name) = mod_name(&message)
        {
            message = format!("mod={name} {message}");
        }
        crate::log(level, source, &message);
    }
}

/// The layer that writes app.log: the app's and the script runtime's events from INFO up, what a mod prints with
/// console.warn or console.error (never its console.log, which may hold anything), and warnings and errors from
/// everything else.
pub fn layer<S: Subscriber + for<'a> LookupSpan<'a>>() -> impl Layer<S> {
    FileLayer.with_filter(
        Targets::new()
            .with_default(Level::WARN)
            .with_target("captain", Level::INFO)
            .with_target("gpui_shell", Level::INFO)
            .with_target("gpui_shell::script", Level::WARN),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sources_are_short_words() {
        assert_eq!(short_source("captain_app"), "app");
        assert_eq!(short_source("captain_app::window::titlebar"), "window");
        assert_eq!(short_source("captain::quit"), "quit");
        assert_eq!(short_source("gpui_shell::engine::quickjs"), "mods");
        assert_eq!(short_source("gpui_shell::script"), "mod-script");
        assert_eq!(short_source("notify::fsevent"), "notify");
    }

    #[test]
    fn a_script_error_names_its_mod() {
        let stack = "error in click handler: Error: boom\n    at save (~/.config/captain-app/mods/captain-data/main.js:3:9)\n    at call (~/.config/captain-app/mods/captain-data/kit/ui.js:1:1)";
        assert_eq!(mod_name(stack), Some("captain-data"));
        assert_eq!(mod_name("render failed\n    at render (~/.config/captain-app/frame/settings/main.js:9)"), Some("settings"));
        assert_eq!(mod_name("no file named here"), None);
    }
}
