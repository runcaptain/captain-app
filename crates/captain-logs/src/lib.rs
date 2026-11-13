//! CAPTAIN: what the app writes down about itself, so that after it stops anyone can say why from a file.
//!
//! - `app.log`: one line per event (`2026-11-13T10:22:33.123Z INFO  window  opened`), a new file every 5 MB,
//!   five old ones kept. Everything the app reports through `tracing` lands here (`layer`).
//! - `crash-<UTC time>.log`: written by the panic hook (`crash`), and for a fatal signal (`signals`).
//! - `run.json`: written at start and removed on a clean quit, so the next start knows how the last run ended
//!   (`run`). `last-run.json` keeps that answer for the Settings buttons and `cst app logs`.
//!
//! Every line goes through `redact`: no keys, tokens or home folders. This crate has no window code, so its tests
//! build in seconds.

pub mod crash;
mod layer;
pub mod redact;
pub mod rotate;
pub mod run;
#[cfg(unix)]
pub mod signals;
pub mod time;

pub use layer::{layer, short_source};

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

/// Picks another folder for the logs. Tests set it, so they never write into the person's real one. The app hands
/// it to the services it starts and to its terminals, so `cst` reads and writes the same folder.
pub const DIR_ENV: &str = "CAPTAIN_LOG_DIR";
pub const APP_LOG: &str = "app.log";
pub const MAX_BYTES: u64 = 5 * 1024 * 1024;
pub const KEEP: usize = 5;
/// How much of app.log a crash record carries.
pub const TAIL_LINES: usize = 200;

/// The platform's usual place: ~/Library/Logs/Captain on macOS, %LOCALAPPDATA%\Captain\Logs on Windows,
/// $XDG_STATE_HOME/captain-app/logs (else ~/.local/state/captain-app/logs) elsewhere.
pub fn platform_dir() -> Option<PathBuf> {
    let var = |k: &str| std::env::var_os(k).filter(|v| !v.is_empty()).map(PathBuf::from);
    if cfg!(target_os = "macos") {
        var("HOME").map(|h| h.join("Library/Logs/Captain"))
    } else if cfg!(windows) {
        var("LOCALAPPDATA").map(|d| d.join("Captain").join("Logs"))
    } else {
        var("XDG_STATE_HOME").or_else(|| var("HOME").map(|h| h.join(".local/state"))).map(|s| s.join("captain-app/logs"))
    }
}

/// Where this run logs. $CAPTAIN_LOG_DIR wins. A window on its own home (a test window, a second Captain) logs
/// inside that home, so it never mixes its lines into the person's real log. Otherwise the platform's place.
pub fn resolve_dir(own_home: Option<&Path>) -> PathBuf {
    if let Some(dir) = std::env::var_os(DIR_ENV).filter(|v| !v.is_empty()) {
        return PathBuf::from(dir);
    }
    match own_home {
        Some(home) => home.join("logs"),
        None => platform_dir().unwrap_or_else(|| std::env::temp_dir().join("captain-logs")),
    }
}

/// Which build is running, for the start line and crash records.
#[derive(Clone, Debug, Default)]
pub struct Build {
    pub version: String,
    pub commit: String,
    pub os: String,
}

/// Lets the same warning or error through once a minute, so a redraw loop that throws cannot flood the file.
pub struct Limiter {
    seen: HashMap<u64, (Instant, u32)>,
    window: Duration,
}

impl Limiter {
    pub fn new(window: Duration) -> Self {
        Self { seen: HashMap::new(), window }
    }

    /// None: seen within the window, drop it. Some(n): write it; n more were dropped since it was last written.
    pub fn check(&mut self, key: u64, now: Instant) -> Option<u32> {
        if self.seen.len() > 2048 {
            let window = self.window;
            self.seen.retain(|_, (at, _)| now.duration_since(*at) < window);
        }
        match self.seen.get_mut(&key) {
            Some((at, dropped)) if now.duration_since(*at) < self.window => {
                *dropped += 1;
                None
            }
            Some(entry) => {
                let dropped = entry.1;
                *entry = (now, 0);
                Some(dropped)
            }
            None => {
                self.seen.insert(key, (now, 0));
                Some(0)
            }
        }
    }
}

struct Logger {
    dir: PathBuf,
    file: Mutex<rotate::RotatingFile>,
    limiter: Mutex<Limiter>,
    home: Option<String>,
    build: Build,
    run: OnceLock<String>,
}

static LOGGER: OnceLock<Logger> = OnceLock::new();

fn home() -> Option<String> {
    std::env::var(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).ok().filter(|h| h.len() > 1)
}

/// Open the log in `dir`. The first call wins; later ones do nothing.
pub fn start(dir: PathBuf, build: Build) {
    let _ = LOGGER.set(Logger {
        file: Mutex::new(rotate::RotatingFile::new(dir.join(APP_LOG), MAX_BYTES, KEEP)),
        limiter: Mutex::new(Limiter::new(Duration::from_secs(60))),
        home: home(),
        dir,
        build,
        run: OnceLock::new(),
    });
}

/// The folder the log is in, once `start` ran.
pub fn dir() -> Option<&'static Path> {
    LOGGER.get().map(|l| l.dir.as_path())
}

pub fn build() -> Option<&'static Build> {
    LOGGER.get().map(|l| &l.build)
}

/// This run's id (start time and pid), once `run::begin` ran: crash records carry it.
pub fn run_id() -> Option<&'static str> {
    LOGGER.get().and_then(|l| l.run.get()).map(String::as_str)
}

pub(crate) fn set_run_id(id: &str) {
    if let Some(l) = LOGGER.get() {
        let _ = l.run.set(id.to_owned());
    }
}

/// One event as text: the time, the level, a short source, the message. Further lines of a message (a stack) are
/// indented, so a line that starts with a time always starts an event.
pub fn format_line(secs: u64, millis: u32, level: &str, source: &str, message: &str) -> String {
    let mut lines = message.trim_end().lines();
    let mut out = format!("{} {level:<5} {source}  {}\n", time::iso(secs, millis), lines.next().unwrap_or(""));
    for line in lines {
        out.push_str("    ");
        out.push_str(line.trim_end());
        out.push('\n');
    }
    out
}

fn key(source: &str, message: &str) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    (source, message).hash(&mut h);
    h.finish()
}

fn write(level: &str, source: &str, message: &str, wait: bool) {
    let Some(logger) = LOGGER.get() else { return };
    let mut message = redact::redact(message, logger.home.as_deref());
    if matches!(level, "WARN" | "ERROR") {
        let lock = if wait { Some(logger.limiter.lock().unwrap_or_else(|e| e.into_inner())) } else { logger.limiter.try_lock().ok() };
        if let Some(mut limiter) = lock {
            match limiter.check(key(source, &message), Instant::now()) {
                None => return,
                Some(0) => {}
                Some(n) => message = format!("{} (and {n} more of the same in the minute before)", message.trim_end()),
            }
        }
    }
    let (secs, millis) = time::now();
    let text = format_line(secs, millis, level, source, &message);
    let lock = if wait { Some(logger.file.lock().unwrap_or_else(|e| e.into_inner())) } else { logger.file.try_lock().ok() };
    if let Some(mut file) = lock {
        let _ = file.write(&text);
    }
}

/// Write one event. `level` is INFO, WARN or ERROR; `source` is a short word for where it came from.
pub fn log(level: &str, source: &str, message: &str) {
    write(level, source, message, true);
}

/// The same from a panic hook: it never waits for a lock the panicking thread may hold.
pub(crate) fn log_without_waiting(level: &str, source: &str, message: &str) {
    write(level, source, message, false);
}

pub fn info(source: &str, message: &str) {
    log("INFO", source, message);
}

pub fn warn(source: &str, message: &str) {
    log("WARN", source, message);
}

pub fn error(source: &str, message: &str) {
    log("ERROR", source, message);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_same_error_is_written_once_a_minute() {
        let mut limiter = Limiter::new(Duration::from_secs(60));
        let start = Instant::now();
        assert_eq!(limiter.check(1, start), Some(0));
        // A redraw loop: the same error 500 times in a few seconds is one line.
        for n in 1..=500 {
            assert_eq!(limiter.check(1, start + Duration::from_millis(n * 10)), None);
        }
        // Another error is not held back by it.
        assert_eq!(limiter.check(2, start + Duration::from_secs(1)), Some(0));
        // A minute later it is written again, and says how many were dropped.
        assert_eq!(limiter.check(1, start + Duration::from_secs(61)), Some(500));
        assert_eq!(limiter.check(1, start + Duration::from_secs(62)), None);
        assert_eq!(limiter.check(1, start + Duration::from_secs(122)), Some(1));
    }

    #[test]
    fn an_event_is_one_line_and_a_stack_is_indented_under_it() {
        assert_eq!(format_line(1_794_565_353, 123, "INFO", "window", "opened"), "2026-11-13T10:22:33.123Z INFO  window  opened\n");
        assert_eq!(
            format_line(1_794_565_353, 0, "ERROR", "mods", "error in click handler: boom\n    at save (main.js:3)\n"),
            "2026-11-13T10:22:33.000Z ERROR mods  error in click handler: boom\n        at save (main.js:3)\n"
        );
    }

    #[test]
    fn a_window_on_its_own_home_logs_inside_it() {
        // (CAPTAIN_LOG_DIR itself is covered by tests/crash_child.rs, which sets it for a child process.)
        if std::env::var_os(DIR_ENV).is_none() {
            assert_eq!(resolve_dir(Some(Path::new("/tmp/home"))), Path::new("/tmp/home/logs"));
        }
    }
}
