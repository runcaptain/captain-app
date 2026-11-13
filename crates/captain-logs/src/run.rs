//! How the last run ended. The app writes `run.json` when it starts and removes it on a clean quit. A start that
//! finds the file still there knows the last run did not end cleanly: it crashed and left a record, a signal ended
//! it, or it vanished with nothing (killed, power loss). The answer is kept in `last-run.json`.

use crate::{Build, crash, time};
use serde_json::{Value, json};
use std::path::Path;

pub const MARKER: &str = "run.json";
pub const LAST: &str = "last-run.json";
/// The end of app.log as the last run left it, kept when that run left no crash record.
pub const LAST_TAIL: &str = "last-run-tail.log";

#[derive(Clone, Debug, PartialEq)]
pub enum Ending {
    /// No earlier run in this folder.
    First,
    Clean,
    /// A panic or a fatal signal, with its record's file name.
    Crashed { record: String },
    /// Ended by a signal sent from outside (SIGTERM, SIGINT, SIGHUP), which the app logged before going.
    Signal { name: String },
    /// Still marked as running, and nothing says why it stopped.
    Vanished,
}

impl Ending {
    /// The word `last-run.json` and `cst app logs` use.
    pub fn word(&self) -> &'static str {
        match self {
            Ending::First => "first",
            Ending::Clean => "clean",
            Ending::Crashed { .. } => "crashed",
            Ending::Signal { .. } => "signal",
            Ending::Vanished => "vanished",
        }
    }

    /// For the log's start line.
    pub fn sentence(&self) -> String {
        match self {
            Ending::First => "no earlier run recorded here".into(),
            Ending::Clean => "ended cleanly".into(),
            Ending::Crashed { record } => format!("crashed, record {record}"),
            Ending::Signal { name } => format!("ended by {name}, sent from outside"),
            Ending::Vanished => "vanished with no record (killed, forced to quit, or the computer lost power)".into(),
        }
    }

    /// True when the person should hear about it: the app went away and nothing asked it to.
    pub fn unexpected(&self) -> bool {
        matches!(self, Ending::Crashed { .. } | Ending::Vanished)
    }
}

fn read(path: &Path) -> Option<Value> {
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

fn write(path: &Path, value: &Value) {
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(path, format!("{value:#}\n"));
}

/// Call once at start, before the first log line of this run: says how the last run ended, records that answer,
/// and marks this run as running. Returns this run's id too.
pub fn begin(dir: &Path, build: &Build, pid: u32, now: (u64, u32)) -> (String, Ending) {
    let id = format!("{}-{pid}", time::compact(now.0));
    let marker = read(&dir.join(MARKER));
    // A fatal signal leaves raw notes its handler could write safely. Turn them into records first.
    let adopted = crash::adopt_fatal(dir);
    let previous = marker.as_ref().and_then(|m| m["run"].as_str()).unwrap_or("");
    let ending = match &marker {
        None if dir.join(crate::APP_LOG).exists() || dir.join(LAST).exists() => Ending::Clean,
        None => Ending::First,
        Some(m) => {
            let record = adopted.iter().find(|(run, _)| run == previous).map(|(_, name)| name.clone()).or_else(|| crash::record_of(dir, previous));
            match (record, m["ended"].as_str()) {
                (Some(record), _) => Ending::Crashed { record },
                (None, Some(name)) => Ending::Signal { name: name.to_owned() },
                (None, None) => Ending::Vanished,
            }
        }
    };
    let _ = std::fs::remove_file(dir.join(LAST_TAIL));
    if matches!(ending, Ending::Signal { .. } | Ending::Vanished) {
        let head = format!(
            "Captain: how the last run ended\nended: {}\nrun: {previous}\nversion: {}\ncommit: {}\nNo crash record was written. These are the last {} lines of app.log from that run.\n\n",
            ending.sentence(),
            marker.as_ref().and_then(|m| m["version"].as_str()).unwrap_or("unknown"),
            marker.as_ref().and_then(|m| m["commit"].as_str()).unwrap_or("unknown"),
            crate::TAIL_LINES,
        );
        let _ = std::fs::write(dir.join(LAST_TAIL), head + &crash::tail(&dir.join(crate::APP_LOG), crate::TAIL_LINES));
    }
    write(&dir.join(LAST), &json!({
        "ending": ending.word(),
        "detail": ending.sentence(),
        "record": match &ending { Ending::Crashed { record } => Value::String(record.clone()), _ => Value::Null },
        "run": previous,
        "started": marker.as_ref().map(|m| m["started"].clone()).unwrap_or(Value::Null),
        "found_at": time::iso(now.0, now.1),
    }));
    write(&dir.join(MARKER), &json!({ "run": id, "pid": pid, "started": time::iso(now.0, now.1), "version": build.version, "commit": build.commit }));
    crate::set_run_id(&id);
    (id, ending)
}

/// The app is quitting the ordinary way: the next start reads this as a clean end.
pub fn end_clean(dir: &Path) {
    let _ = std::fs::remove_file(dir.join(MARKER));
}

/// A signal from outside is ending the app: the next start says so instead of "vanished".
pub fn mark_signal(dir: &Path, name: &str) {
    if let Some(mut marker) = read(&dir.join(MARKER)) {
        marker["ended"] = Value::String(name.to_owned());
        write(&dir.join(MARKER), &marker);
    }
}

/// What `begin` found, for the Settings buttons and the notice: `{ending, detail, record, ...}`.
pub fn last(dir: &Path) -> Option<Value> {
    read(&dir.join(LAST))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("captain-logs-run-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn build() -> Build {
        Build { version: "0.1.13".into(), commit: "31aec7ff".into(), os: "test".into() }
    }

    #[test]
    fn the_next_start_knows_how_the_last_run_ended() {
        let dir = scratch("endings");
        // Nothing here yet.
        let (first, ending) = begin(&dir, &build(), 100, (1_794_565_353, 0));
        assert_eq!((first.as_str(), &ending), ("20261113T102233Z-100", &Ending::First));
        assert!(dir.join(MARKER).exists());

        // A clean quit removes the mark.
        end_clean(&dir);
        let (_, ending) = begin(&dir, &build(), 101, (1_794_565_400, 0));
        assert_eq!(ending, Ending::Clean);
        assert_eq!(last(&dir).unwrap()["ending"], "clean");
        assert!(!dir.join(LAST_TAIL).exists());

        // Killed: the mark is still there and nothing says why. The log's last lines are kept to copy.
        std::fs::write(dir.join(crate::APP_LOG), "2026-11-13T10:23:20.000Z INFO  window  opened\n2026-11-13T10:23:21.000Z INFO  services  ready\n").unwrap();
        let (_, ending) = begin(&dir, &build(), 102, (1_794_565_500, 0));
        assert_eq!(ending, Ending::Vanished);
        assert!(ending.unexpected());
        let saved = last(&dir).unwrap();
        assert_eq!((saved["ending"].as_str(), saved["run"].as_str()), (Some("vanished"), Some("20261113T102320Z-101")));
        let tail = std::fs::read_to_string(dir.join(LAST_TAIL)).unwrap();
        assert!(tail.contains("vanished with no record") && tail.ends_with("INFO  services  ready\n"), "{tail}");

        // A crash: the panic hook left a record that names the run.
        let record = crash::write_record(&dir, 1_794_565_550, &build(), "20261113T102500Z-102", &[("kind", "panic".into()), ("message", "boom".into())], "", "").unwrap();
        let (_, ending) = begin(&dir, &build(), 103, (1_794_565_600, 0));
        let name = record.file_name().unwrap().to_string_lossy().into_owned();
        assert_eq!(ending, Ending::Crashed { record: name.clone() });
        assert_eq!(last(&dir).unwrap()["record"], name);
        assert!(!dir.join(LAST_TAIL).exists(), "a crash has its record; no separate tail");

        // A signal from outside, logged before the app went.
        mark_signal(&dir, "SIGTERM");
        let (_, ending) = begin(&dir, &build(), 104, (1_794_565_700, 0));
        assert_eq!(ending, Ending::Signal { name: "SIGTERM".into() });
        assert!(!ending.unexpected());
        assert!(std::fs::read_to_string(dir.join(LAST_TAIL)).unwrap().contains("ended by SIGTERM"));

        // An older record of another run does not make a killed run look like a crash.
        let (_, ending) = begin(&dir, &build(), 105, (1_794_565_800, 0));
        assert_eq!(ending, Ending::Vanished);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
