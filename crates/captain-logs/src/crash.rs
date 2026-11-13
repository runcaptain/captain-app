//! Crash records: `crash-<UTC time>.log`, one per crash, with what went wrong, where, the build, and the end of
//! app.log. Written by the panic hook, and at the next start for a fatal signal (see `signals`).

use crate::{Build, redact, time};
use std::path::{Path, PathBuf};

/// The last `lines` lines of the log at `path` (and of the file before it, when this one is too new to have them).
pub fn tail(path: &Path, lines: usize) -> String {
    let read = |p: &Path| -> Vec<String> {
        use std::io::{Read, Seek, SeekFrom};
        let Ok(mut file) = std::fs::File::open(p) else { return Vec::new() };
        let len = file.metadata().map(|m| m.len()).unwrap_or(0);
        const MOST: u64 = 512 * 1024;
        let from = len.saturating_sub(MOST);
        let mut bytes = Vec::new();
        if file.seek(SeekFrom::Start(from)).is_err() || file.read_to_end(&mut bytes).is_err() {
            return Vec::new();
        }
        let text = String::from_utf8_lossy(&bytes);
        // Cut in the middle of the file: the first piece is half a line.
        text.lines().skip(usize::from(from > 0)).map(str::to_owned).collect()
    };
    let mut all = read(path);
    if all.len() < lines {
        let mut before = read(&crate::rotate::numbered(path, 1));
        before.append(&mut all);
        all = before;
    }
    let start = all.len().saturating_sub(lines);
    let mut out = all[start..].join("\n");
    if !out.is_empty() {
        out.push('\n');
    }
    out
}

/// Crash records in `dir`, oldest first.
pub fn records(dir: &Path) -> Vec<PathBuf> {
    let mut found: Vec<PathBuf> = std::fs::read_dir(dir).into_iter().flatten().flatten().map(|e| e.path())
        .filter(|p| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("crash-") && n.ends_with(".log")))
        .collect();
    found.sort();
    found
}

/// The file name of the newest record written by run `run`, if it left one.
pub fn record_of(dir: &Path, run: &str) -> Option<String> {
    if run.is_empty() {
        return None;
    }
    let line = format!("run: {run}");
    records(dir).into_iter().rev().find(|p| std::fs::read_to_string(p).is_ok_and(|text| text.lines().take(12).any(|l| l == line)))
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
}

/// Write a record. `facts` are `name: value` lines after the build's; `trace` is a backtrace; `log_tail` the end of
/// app.log. Everything goes through redaction. Returns the file.
pub fn write_record(dir: &Path, secs: u64, build: &Build, run: &str, facts: &[(&str, String)], trace: &str, log_tail: &str) -> std::io::Result<PathBuf> {
    std::fs::create_dir_all(dir)?;
    let mut text = format!("Captain crash record\ntime: {}\nversion: {}\ncommit: {}\nos: {}\nrun: {run}\n", time::iso(secs, 0), build.version, build.commit, build.os);
    for (name, value) in facts {
        text.push_str(&format!("{name}: {}\n", value.trim_end()));
    }
    text.push_str("\n---- backtrace ----\n");
    text.push_str(trace.trim_end());
    text.push_str(&format!("\n\n---- last {} lines of app.log ----\n", crate::TAIL_LINES));
    text.push_str(log_tail);
    let home = std::env::var(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).ok();
    let text = redact::redact(&text, home.as_deref());
    let stamp = time::compact(secs);
    let mut path = dir.join(format!("crash-{stamp}.log"));
    let mut n = 2;
    while path.exists() {
        path = dir.join(format!("crash-{stamp}-{n}.log"));
        n += 1;
    }
    std::fs::write(&path, text)?;
    Ok(path)
}

fn record_panic(info: &std::panic::PanicHookInfo<'_>) {
    let (Some(dir), Some(build)) = (crate::dir(), crate::build()) else { return };
    let message = info.payload().downcast_ref::<&str>().map(|s| (*s).to_owned())
        .or_else(|| info.payload().downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "a panic with no message".into());
    let location = info.location().map(|l| format!("{}:{}:{}", l.file(), l.line(), l.column())).unwrap_or_else(|| "unknown".into());
    let thread = std::thread::current();
    let thread = thread.name().map(str::to_owned).unwrap_or_else(|| format!("{:?}", thread.id()));
    let trace = std::backtrace::Backtrace::force_capture().to_string();
    let facts = [("kind", "panic".to_owned()), ("thread", thread.clone()), ("message", message.clone()), ("location", location.clone())];
    let (secs, _) = time::now();
    let written = write_record(dir, secs, build, crate::run_id().unwrap_or(""), &facts, &trace, &tail(&dir.join(crate::APP_LOG), crate::TAIL_LINES));
    let record = match &written {
        Ok(path) => format!("record {}", path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()),
        Err(e) => format!("no record could be written: {e}"),
    };
    crate::log_without_waiting("ERROR", "crash", &format!("panic on thread {thread} at {location}: {message} ({record})"));
}

/// From now on a panic anywhere writes a crash record and a line in app.log, then goes on as it would have (the
/// hook that was there before still runs, so the message still reaches stderr and the process ends the same way).
pub fn install_panic_hook() {
    let before = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        record_panic(info);
        before(info);
    }));
}

/// Turn the raw notes a fatal signal left (`fatal-<run>.raw`, see `signals`) into crash records, with the end of
/// app.log, which is still the crashed run's at this point. Returns `(run, record file name)` for each.
pub fn adopt_fatal(dir: &Path) -> Vec<(String, String)> {
    let mut adopted = Vec::new();
    for entry in std::fs::read_dir(dir).into_iter().flatten().flatten() {
        let path = entry.path();
        let Some(run) = path.file_name().and_then(|n| n.to_str()).and_then(|n| n.strip_prefix("fatal-")?.strip_suffix(".raw")).map(str::to_owned) else { continue };
        let Ok(raw) = std::fs::read_to_string(&path) else { continue };
        let fact = |name: &str| raw.lines().find_map(|l| l.strip_prefix(name)?.strip_prefix(": ")).unwrap_or("unknown").to_owned();
        let secs = fact("epoch").parse::<u64>().unwrap_or_else(|_| time::now().0);
        let build = Build { version: fact("version"), commit: fact("commit"), os: fact("os") };
        let trace = raw.split_once("---- backtrace ----\n").map(|(_, t)| t).unwrap_or("");
        let facts = [("kind", "fatal signal".to_owned()), ("signal", fact("signal"))];
        if let Ok(record) = write_record(dir, secs, &build, &run, &facts, trace, &tail(&dir.join(crate::APP_LOG), crate::TAIL_LINES)) {
            adopted.push((run, record.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()));
            let _ = std::fs::remove_file(&path);
        }
    }
    adopted
}

/// What to copy for "what happened last time": the crash record when the last run crashed, the end of its log
/// when it stopped without one, and otherwise the end of the log as it is now.
pub fn what_happened(dir: &Path) -> String {
    let last = crate::run::last(dir);
    let ending = last.as_ref().and_then(|l| l["ending"].as_str()).unwrap_or("");
    let read = |name: &str| std::fs::read_to_string(dir.join(name)).ok().filter(|t| !t.is_empty());
    if ending == "crashed"
        && let Some(text) = last.as_ref().and_then(|l| l["record"].as_str()).and_then(read)
    {
        return text;
    }
    if matches!(ending, "signal" | "vanished")
        && let Some(text) = read(crate::run::LAST_TAIL)
    {
        return text;
    }
    let mut out = String::from("Captain: the last run ended normally.\n");
    if let Some(older) = records(dir).last().and_then(|p| std::fs::read_to_string(p).ok()) {
        out.push_str("The most recent crash record is from an earlier run:\n\n");
        out.push_str(&older);
        return out;
    }
    out.push_str(&format!("No crash record exists. These are the last {} lines of app.log.\n\n", crate::TAIL_LINES));
    out.push_str(&tail(&dir.join(crate::APP_LOG), crate::TAIL_LINES));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("captain-logs-crash-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn the_tail_is_the_last_lines_even_across_a_new_file() {
        let dir = scratch("tail");
        let log = dir.join("app.log");
        std::fs::write(crate::rotate::numbered(&log, 1), "old 1\nold 2\nold 3\n").unwrap();
        std::fs::write(&log, "new 1\nnew 2\n").unwrap();
        assert_eq!(tail(&log, 1), "new 2\n");
        assert_eq!(tail(&log, 2), "new 1\nnew 2\n");
        assert_eq!(tail(&log, 4), "old 2\nold 3\nnew 1\nnew 2\n");
        assert_eq!(tail(&dir.join("missing.log"), 5), "");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_record_carries_no_secret_and_two_in_one_second_do_not_collide() {
        let dir = scratch("record");
        let build = Build { version: "0.1.13".into(), commit: "31aec7ff".into(), os: "test".into() };
        let facts = [("kind", "panic".to_owned()), ("message", "upload failed: token=abababababab".to_owned())];
        let one = write_record(&dir, 1_794_565_353, &build, "r1", &facts, "0: main\n", "a line\n").unwrap();
        let two = write_record(&dir, 1_794_565_353, &build, "r2", &facts, "", "").unwrap();
        assert_eq!(one.file_name().unwrap(), "crash-20261113T102233Z.log");
        assert_eq!(two.file_name().unwrap(), "crash-20261113T102233Z-2.log");
        let text = std::fs::read_to_string(&one).unwrap();
        assert!(text.contains("version: 0.1.13\ncommit: 31aec7ff\n") && text.contains("message: upload failed: token=[redacted]") && text.ends_with("a line\n"), "{text}");
        assert!(!text.contains("abababab"));
        assert_eq!(record_of(&dir, "r1").as_deref(), Some("crash-20261113T102233Z.log"));
        assert_eq!(record_of(&dir, "r3"), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_fatal_signals_note_becomes_a_record_at_the_next_start() {
        let dir = scratch("fatal");
        std::fs::write(dir.join("app.log"), "2026-11-13T10:22:30.000Z INFO  window  opened\n").unwrap();
        std::fs::write(dir.join("fatal-20261113T102200Z-77.raw"), "version: 0.1.13\ncommit: 31aec7ff\nos: test\nsignal: 11\nepoch: 1794565353\n---- backtrace ----\n0   captain-app  0x0000000100000000 main + 12\n").unwrap();
        let adopted = adopt_fatal(&dir);
        assert_eq!(adopted, vec![("20261113T102200Z-77".to_owned(), "crash-20261113T102233Z.log".to_owned())]);
        let text = std::fs::read_to_string(dir.join(&adopted[0].1)).unwrap();
        assert!(text.contains("kind: fatal signal\nsignal: 11\n") && text.contains("main + 12") && text.contains("INFO  window  opened"), "{text}");
        assert!(!dir.join("fatal-20261113T102200Z-77.raw").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn what_happened_is_the_record_or_the_end_of_the_log() {
        let dir = scratch("what");
        let build = Build { version: "0.1.13".into(), commit: "31aec7ff".into(), os: "test".into() };
        std::fs::write(dir.join("app.log"), "2026-11-13T10:22:30.000Z INFO  window  opened\n").unwrap();
        assert!(what_happened(&dir).contains("ended normally") && what_happened(&dir).contains("INFO  window  opened"));
        // Killed.
        crate::run::begin(&dir, &build, 1, (1_794_565_353, 0));
        crate::run::begin(&dir, &build, 2, (1_794_565_400, 0));
        assert!(what_happened(&dir).contains("vanished with no record"));
        // Crashed.
        write_record(&dir, 1_794_565_420, &build, "20261113T102320Z-2", &[("message", "boom".into())], "", "").unwrap();
        crate::run::begin(&dir, &build, 3, (1_794_565_500, 0));
        assert!(what_happened(&dir).starts_with("Captain crash record") && what_happened(&dir).contains("message: boom"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
