//! What a real process leaves behind. Each test starts this test program again as a child that plays the app
//! (CAPTAIN_LOGS_CHILD says which part), with CAPTAIN_LOG_DIR pointing at a scratch folder, and reads the files.

use captain_logs::{Build, crash, run};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

fn build() -> Build {
    Build { version: "0.1.13".into(), commit: "31aec7ff".into(), os: "test".into() }
}

/// The child's side: start the log the way the app does, then do what the test asked.
fn play(part: &str) {
    let dir = captain_logs::resolve_dir(None);
    captain_logs::start(dir.clone(), build());
    let (id, ending) = run::begin(&dir, &build(), std::process::id(), captain_logs::time::now());
    captain_logs::info("app", &format!("started; last run: {}", ending.sentence()));
    crash::install_panic_hook();
    #[cfg(unix)]
    {
        captain_logs::signals::watch_fatal_signals(&dir, &id, "version: 0.1.13\ncommit: 31aec7ff\nos: test\n");
        let dir = dir.clone();
        captain_logs::signals::watch_quit_signals(move |name| {
            captain_logs::warn("quit", &format!("quit: {name} received from outside; ending now"));
            run::mark_signal(&dir, name);
        });
    }
    let _ = id;
    match part {
        "panic" => {
            captain_logs::info("window", "opened the main window");
            // The app's own key is in the message on purpose: it must not reach the record.
            std::thread::Builder::new().name("worker".into()).spawn(|| panic!("lost the connection: token=abababababab")).unwrap().join().ok();
            std::process::exit(101);
        }
        "clean" => run::end_clean(&dir),
        "wait" => {
            println!("ready");
            std::thread::sleep(std::time::Duration::from_secs(30));
        }
        "abort" => std::process::abort(),
        other => panic!("unknown part {other}"),
    }
}

fn child(part: &str, dir: &Path) -> Command {
    let mut cmd = Command::new(std::env::current_exe().unwrap());
    cmd.args(["--exact", "child_entry", "--nocapture"]).env("CAPTAIN_LOGS_CHILD", part).env(captain_logs::DIR_ENV, dir).stderr(Stdio::null());
    cmd
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("captain-logs-child-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

/// Not a test of its own: the door the child comes in through.
#[test]
fn child_entry() {
    if let Ok(part) = std::env::var("CAPTAIN_LOGS_CHILD") {
        play(&part);
    }
}

#[test]
fn a_panic_leaves_a_crash_record_and_the_next_start_says_so() {
    let dir = scratch("panic");
    let status = child("panic", &dir).stdout(Stdio::null()).status().unwrap();
    assert_eq!(status.code(), Some(101), "the process still ends its own way");
    let records = crash::records(&dir);
    assert_eq!(records.len(), 1, "{records:?}");
    let text = std::fs::read_to_string(&records[0]).unwrap();
    for want in ["Captain crash record", "version: 0.1.13", "commit: 31aec7ff", "kind: panic", "thread: worker", "message: lost the connection: token=[redacted]", "location: ", "crash_child.rs", "---- backtrace ----", "---- last 200 lines of app.log ----", "INFO  window  opened the main window"] {
        assert!(text.contains(want), "missing {want:?} in:\n{text}");
    }
    assert!(!text.contains("abababab"), "the secret reached the record");
    let log = std::fs::read_to_string(dir.join("app.log")).unwrap();
    assert!(log.contains("ERROR crash  panic on thread worker") && !log.contains("abababab"), "{log}");
    // The next start: crashed, with that record.
    let (_, ending) = run::begin(&dir, &build(), 1, captain_logs::time::now());
    assert_eq!(ending, run::Ending::Crashed { record: records[0].file_name().unwrap().to_string_lossy().into_owned() });
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_clean_quit_is_clean_at_the_next_start() {
    let dir = scratch("clean");
    assert!(child("clean", &dir).stdout(Stdio::null()).status().unwrap().success());
    assert!(!dir.join(run::MARKER).exists());
    let (_, ending) = run::begin(&dir, &build(), 1, captain_logs::time::now());
    assert_eq!(ending, run::Ending::Clean);
    assert!(crash::records(&dir).is_empty());
    let _ = std::fs::remove_dir_all(&dir);
}

#[cfg(unix)]
fn wait_until_ready(dir: &Path, part: &str) -> std::process::Child {
    use std::io::{BufRead, BufReader};
    let mut running = child(part, dir).stdout(Stdio::piped()).spawn().unwrap();
    let mut lines = BufReader::new(running.stdout.take().unwrap()).lines();
    assert!(lines.any(|l| l.is_ok_and(|l| l.contains("ready"))), "the child never got going");
    running
}

#[cfg(unix)]
#[test]
fn a_signal_from_outside_is_logged_before_the_app_goes() {
    use std::os::unix::process::ExitStatusExt;
    let dir = scratch("sigterm");
    let mut running = wait_until_ready(&dir, "wait");
    unsafe { libc::kill(running.id() as i32, libc::SIGTERM) };
    let status = running.wait().unwrap();
    assert_eq!(status.signal(), Some(libc::SIGTERM), "it still ends by the signal");
    let log = std::fs::read_to_string(dir.join("app.log")).unwrap();
    assert!(log.contains("WARN  quit  quit: SIGTERM received from outside"), "{log}");
    let (_, ending) = run::begin(&dir, &build(), 1, captain_logs::time::now());
    assert_eq!(ending, run::Ending::Signal { name: "SIGTERM".into() });
    let _ = std::fs::remove_dir_all(&dir);
}

#[cfg(unix)]
#[test]
fn a_kill_nothing_can_catch_reads_as_vanished() {
    let dir = scratch("sigkill");
    let mut running = wait_until_ready(&dir, "wait");
    unsafe { libc::kill(running.id() as i32, libc::SIGKILL) };
    running.wait().unwrap();
    let (_, ending) = run::begin(&dir, &build(), 1, captain_logs::time::now());
    assert_eq!(ending, run::Ending::Vanished);
    assert!(crash::what_happened(&dir).contains("vanished with no record"));
    let _ = std::fs::remove_dir_all(&dir);
}

#[cfg(unix)]
#[test]
fn a_crash_below_rust_leaves_a_record_too() {
    use std::os::unix::process::ExitStatusExt;
    let dir = scratch("abort");
    let status = child("abort", &dir).stdout(Stdio::null()).status().unwrap();
    assert_eq!(status.signal(), Some(libc::SIGABRT), "it still dies by the signal, so the system's crash report is made");
    let (_, ending) = run::begin(&dir, &build(), 1, captain_logs::time::now());
    let run::Ending::Crashed { record } = ending else { panic!("expected a crash, got {ending:?}") };
    let text = std::fs::read_to_string(dir.join(record)).unwrap();
    assert!(text.contains("kind: fatal signal") && text.contains(&format!("signal: {}", libc::SIGABRT)) && text.contains("---- backtrace ----"), "{text}");
    let _ = std::fs::remove_dir_all(&dir);
}
