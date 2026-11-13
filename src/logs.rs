//! CAPTAIN: the app's log, crash records and run marker (crates/captain-logs does the writing; this is where the
//! app says what happens to it). After the app stops, anyone must be able to say why from a file:
//! ~/Library/Logs/Captain/app.log on a Mac. CAPTAIN_LOG_DIR picks another folder (tests), and a window on its own
//! home (CAPTAIN_APP_HOME, a test window) logs inside that home, never into the person's real log.
//!
//! No secrets: every line is redacted on the way to the file (captain_logs::redact). Still, never hand a token, a
//! request body, query text or a file's contents to tracing in the first place.

use captain_logs::run::Ending;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

static LAST: OnceLock<Ending> = OnceLock::new();
static QUIT: Mutex<Option<String>> = Mutex::new(None);
static NOTICE_DONE: AtomicBool = AtomicBool::new(false);

/// The folder this run logs into.
pub fn dir() -> PathBuf {
    captain_logs::dir().map(|d| d.to_owned()).unwrap_or_else(resolve)
}

fn resolve() -> PathBuf {
    let own = std::env::var_os(crate::brand::HOME_ENV).is_some_and(|v| !v.is_empty()) || crate::test_window();
    captain_logs::resolve_dir(own.then(crate::mods::app_dir).as_deref())
}

fn os() -> String {
    #[cfg(target_os = "macos")]
    if let Some(v) = std::process::Command::new("/usr/bin/sw_vers").arg("-productVersion").output().ok().filter(|o| o.status.success()) {
        return format!("macOS {} {}", String::from_utf8_lossy(&v.stdout).trim(), std::env::consts::ARCH);
    }
    format!("{} {}", std::env::consts::OS, std::env::consts::ARCH)
}

/// How this process was started, as far as it can tell: by the system (Finder, the Dock, `open`), or by a program.
fn launched() -> String {
    let test = if crate::test_window() { ", as a test window" } else { "" };
    #[cfg(unix)]
    let how = {
        use std::io::IsTerminal;
        // SAFETY: getppid has no preconditions.
        let parent = unsafe { libc::getppid() };
        if parent == 1 {
            "launched by the system (Finder, the Dock, `open` or a login item)".to_owned()
        } else {
            let name = std::process::Command::new("/bin/ps").args(["-o", "comm=", "-p", &parent.to_string()]).output().ok()
                .map(|o| String::from_utf8_lossy(&o.stdout).trim().rsplit('/').next().unwrap_or("").to_owned())
                .filter(|n| !n.is_empty())
                .unwrap_or_else(|| "an unknown program".into());
            let terminal = if std::io::stderr().is_terminal() { ", from a terminal" } else { "" };
            format!("launched by {name} (pid {parent}){terminal}")
        }
    };
    #[cfg(not(unix))]
    let how = "launched".to_owned();
    format!("{how}{test}")
}

/// Open the log and send `tracing` to it (and to stderr, as before). Call once, before anything logs.
pub fn open() {
    use tracing_subscriber::prelude::*;
    let build = captain_logs::Build { version: env!("CARGO_PKG_VERSION").into(), commit: env!("CAPTAIN_BUILD_COMMIT").into(), os: os() };
    captain_logs::start(resolve(), build);
    let stderr = tracing_subscriber::fmt::layer().with_writer(std::io::stderr).with_filter(
        tracing_subscriber::EnvFilter::try_from_env("HERDR_APP_LOG").unwrap_or_else(|_| "warn,captain_app=info,gpui_shell=info".into()),
    );
    let _ = tracing_subscriber::registry().with(stderr).with(captain_logs::layer()).try_init();
}

/// This run begins: say how the last one ended, mark this one as running, and from here on write a crash record
/// for a panic or a fatal signal, and a line for a signal that asks the app to stop.
pub fn begin() {
    let (Some(dir), Some(build)) = (captain_logs::dir(), captain_logs::build()) else { return };
    let (run, ending) = captain_logs::run::begin(dir, build, std::process::id(), captain_logs::time::now());
    tracing::info!(target: "captain::app", "started: {} {} (commit {}), {}, {}, pid {}, run {run}", crate::brand::NAME, build.version, build.commit, build.os, launched(), std::process::id());
    if ending.unexpected() {
        tracing::warn!(target: "captain::app", "last run: {}", ending.sentence());
    } else {
        tracing::info!(target: "captain::app", "last run: {}", ending.sentence());
    }
    captain_logs::crash::install_panic_hook();
    #[cfg(unix)]
    {
        let header = format!("version: {}\ncommit: {}\nos: {}\n", build.version, build.commit, build.os);
        captain_logs::signals::watch_fatal_signals(dir, &run, &header);
        captain_logs::signals::watch_quit_signals(move |name| {
            captain_logs::warn("quit", &format!("quit: {name} received (sent by another program or the system); ending now without the usual cleanup"));
            captain_logs::run::mark_signal(dir, name);
        });
    }
    let _ = LAST.set(ending);
}

/// Something in the app is about to ask it to quit: remember what, so the quit line can say. The first one wins.
pub fn quit_asked(what: &str) {
    let mut asked = QUIT.lock().unwrap_or_else(|e| e.into_inner());
    if asked.is_none() {
        *asked = Some(what.to_owned());
    }
}

/// The program that sent the Apple event being handled now, if the quit came as one: `osascript`, the Dock's Quit,
/// the system at log out. None when no Apple event is being handled.
#[cfg(target_os = "macos")]
fn apple_event_sender() -> Option<String> {
    use objc2::runtime::AnyObject;
    use objc2::{class, msg_send};
    // 'spid': keySenderPIDAttr, the sender's process id.
    const SENDER_PID: u32 = u32::from_be_bytes(*b"spid");
    // SAFETY: AppKit calls on the main thread (the quit callbacks run there); every pointer is checked for nil.
    let pid: i32 = unsafe {
        let manager: *mut AnyObject = msg_send![class!(NSAppleEventManager), sharedAppleEventManager];
        if manager.is_null() {
            return None;
        }
        let event: *mut AnyObject = msg_send![manager, currentAppleEvent];
        if event.is_null() {
            return None;
        }
        let sender: *mut AnyObject = msg_send![event, attributeDescriptorForKeyword: SENDER_PID];
        if sender.is_null() {
            return Some("a program that did not give its process id".into());
        }
        msg_send![sender, int32Value]
    };
    let name = std::process::Command::new("/bin/ps").args(["-o", "comm=", "-p", &pid.to_string()]).output().ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().rsplit('/').next().unwrap_or("").to_owned())
        .filter(|n| !n.is_empty());
    Some(match name.as_deref() {
        Some("Dock") => format!("the Dock's Quit item (pid {pid})"),
        Some("loginwindow") => format!("the system, at log out, restart or shut down (loginwindow, pid {pid})"),
        Some(name) => format!("{name} (pid {pid})"),
        None => format!("pid {pid}, which has already gone"),
    })
}

/// The app is quitting (the quit callback's first line): log what asked. With nothing of ours on record, the
/// request came from outside.
pub fn quitting() {
    let asked = QUIT.lock().unwrap_or_else(|e| e.into_inner()).take();
    match asked {
        Some(what) => tracing::info!(target: "captain::quit", "quit: {what}"),
        None => {
            #[cfg(target_os = "macos")]
            let sender = apple_event_sender();
            #[cfg(not(target_os = "macos"))]
            let sender: Option<String> = None;
            match sender {
                Some(who) => tracing::warn!(target: "captain::quit", "quit: asked by another program: {who}"),
                None => tracing::warn!(target: "captain::quit", "quit: asked by another program or the system (no menu, key, button or command of the app asked for it)"),
            }
        }
    }
}

/// The quit callback's last line: this run ended cleanly.
pub fn quit_done() {
    tracing::info!(target: "captain::quit", "quit: done, this run ended cleanly");
    if let Some(dir) = captain_logs::dir() {
        captain_logs::run::end_clean(dir);
    }
}

static HIDDEN: AtomicBool = AtomicBool::new(false);

/// The close button hid the app (macOS).
pub fn hidden() {
    HIDDEN.store(true, Ordering::Relaxed);
}

/// True once, when the window is active again after the close button hid it.
pub fn shown_again() -> bool {
    HIDDEN.swap(false, Ordering::Relaxed)
}

/// True while the "closed unexpectedly last time" line is still to be shown: the last run crashed or vanished, and
/// the person has not dismissed the line or copied what happened.
pub fn notice_due() -> bool {
    !NOTICE_DONE.load(Ordering::Relaxed) && LAST.get().is_some_and(Ending::unexpected)
}

pub fn notice_done() {
    NOTICE_DONE.store(true, Ordering::Relaxed);
}

/// What to copy for "what happened last time": the last run's crash record, else the end of the log.
pub fn what_happened() -> String {
    captain_logs::crash::what_happened(&dir())
}

/// A mod's version for the log: mod.json's "version" when it has one, else the first eight hex digits of its
/// main.js's SHA-256, which says exactly which code ran.
pub fn mod_version(dir: &std::path::Path) -> String {
    let named = std::fs::read_to_string(dir.join("mod.json")).ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .and_then(|v| v.get("version").and_then(|v| v.as_str().map(str::to_owned)));
    if let Some(version) = named {
        return version;
    }
    use sha2::{Digest, Sha256};
    match std::fs::read(dir.join("main.js")) {
        Ok(code) => format!("main.js {}", Sha256::digest(&code).iter().take(4).map(|b| format!("{b:02x}")).collect::<String>()),
        Err(_) => "unknown".into(),
    }
}

/// Save the bug-report zip by asking the app's own cst (`cst app logs --bundle`), which redacts every file once
/// more on the way in. Returns the zip's path. Blocking: call it off the main thread.
#[cfg(unix)]
pub fn save_bundle() -> Result<String, String> {
    let cst = crate::cst::find().ok_or("no cst to make the bundle with")?;
    let out = std::process::Command::new(cst).args(["app", "logs", "--bundle"]).env(captain_logs::DIR_ENV, dir()).stdin(std::process::Stdio::null()).output().map_err(|e| e.to_string())?;
    let path = String::from_utf8_lossy(&out.stdout).lines().last().unwrap_or("").trim().to_owned();
    if out.status.success() && std::path::Path::new(&path).is_file() {
        Ok(path)
    } else {
        Err(format!("cst app logs --bundle failed ({}): {}", out.status, String::from_utf8_lossy(&out.stderr).trim()))
    }
}

#[cfg(not(unix))]
pub fn save_bundle() -> Result<String, String> {
    Err("not available on this system".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_mod_is_logged_with_a_version() {
        let dir = std::env::temp_dir().join(format!("captain-mod-version-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(mod_version(&dir), "unknown");
        std::fs::write(dir.join("main.js"), "export default class {}\n").unwrap();
        let hashed = mod_version(&dir);
        assert!(hashed.starts_with("main.js ") && hashed.len() == 16, "{hashed}");
        std::fs::write(dir.join("mod.json"), r#"{"title": "Data", "version": "2.1.0"}"#).unwrap();
        assert_eq!(mod_version(&dir), "2.1.0");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_first_thing_that_asked_to_quit_is_the_one_logged() {
        quit_asked("the Quit menu item");
        quit_asked("a later one");
        assert_eq!(QUIT.lock().unwrap().as_deref(), Some("the Quit menu item"));
        QUIT.lock().unwrap().take();
    }
}
