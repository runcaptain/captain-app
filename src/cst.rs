//! CAPTAIN: the Captain Search daemon (`cst serve`) behind the webview blocks.
//!
//! A webview URL may say `{cst}` (the daemon's base URL, `http://127.0.0.1:<port>`)
//! and `{cst_token}` (its bearer token). The first time one is needed, Captain
//! starts `cst serve --port 0`, reads its first stdout line (`{"port", "token",
//! "pid"}`) and fills them in. The daemon runs in its own process group and is
//! stopped with the app. A small watcher process stops it too when the app dies
//! by any route (a crash, SIGKILL), because it holds a pipe only the app writes.
//! Ported from the Captain Search Tools Tauri shell (app/src-tauri/src/lib.rs).
//!
//! A daemon that is already up is reused: `cst serve` writes `serve.json`
//! (`{port, token, pid, ui}`) in its home (`$CST_HOME`, else
//! ~/.config/captain-search), and the Tauri app's daemon serves the same
//! workspace. Captain only ever stops a daemon it started itself.

use serde_json::Value;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

pub const INSTALL: &str = "uv tool install captain-search";

#[derive(Clone, Debug, PartialEq)]
pub enum State {
    Idle,
    Starting,
    Ready { port: u16, token: String },
    Missing,
    Failed(String),
}

struct Daemon {
    state: State,
    child: Option<Child>,
    guard: Option<Child>,
}

static DAEMON: Mutex<Daemon> = Mutex::new(Daemon { state: State::Idle, child: None, guard: None });

pub fn state() -> State {
    DAEMON.lock().unwrap().state.clone()
}

fn is_executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    p.is_file() && p.metadata().map(|m| m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

/// $CST_BIN, then PATH, then ~/.local/bin (an app opened from the Dock has a short PATH).
fn find() -> Option<PathBuf> {
    if let Some(b) = std::env::var_os("CST_BIN").map(PathBuf::from).filter(|b| is_executable(b)) {
        return Some(b);
    }
    let on_path = std::env::var_os("PATH").into_iter().flat_map(|p| std::env::split_paths(&p).collect::<Vec<_>>()).map(|d| d.join("cst"));
    let local = std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/bin/cst"));
    on_path.chain(local).find(|c| is_executable(c))
}

/// The first stdout line of `cst serve --port 0`.
pub fn parse_discovery(line: &str) -> Option<(u16, String)> {
    let v: Value = serde_json::from_str(line.trim()).ok()?;
    let port = u16::try_from(v.get("port")?.as_u64()?).ok().filter(|p| *p > 0)?;
    let token = v.get("token")?.as_str()?.to_owned();
    Some((port, token))
}

fn serve_json() -> Option<PathBuf> {
    let home = std::env::var_os("CST_HOME").filter(|v| !v.is_empty()).map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config/captain-search")))?;
    Some(home.join("serve.json"))
}

/// `serve.json` as `(port, token, pid)`.
pub fn parse_serve_json(text: &str) -> Option<(u16, String, i32)> {
    let (port, token) = parse_discovery(text)?;
    let v: Value = serde_json::from_str(text).ok()?;
    let pid = i32::try_from(v.get("pid")?.as_u64()?).ok().filter(|p| *p > 0)?;
    Some((port, token, pid))
}

/// A daemon someone else started that is still up: its pid lives and its port answers.
fn running() -> Option<(u16, String)> {
    let (port, token, pid) = parse_serve_json(&std::fs::read_to_string(serve_json()?).ok()?)?;
    if unsafe { libc::kill(pid, 0) } != 0 {
        return None;
    }
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    std::net::TcpStream::connect_timeout(&addr, Duration::from_millis(300)).ok()?;
    Some((port, token))
}

/// Start the daemon once, in the background, unless one is up. Calls `ready` when its state settles.
pub fn ensure(ready: impl Fn() + Send + 'static) {
    let mut d = DAEMON.lock().unwrap();
    if d.state != State::Idle {
        return;
    }
    if let Some((port, token)) = running() {
        // Not ours: reuse it, never stop it.
        d.state = State::Ready { port, token };
        drop(d);
        ready();
        return;
    }
    let Some(bin) = find() else {
        d.state = State::Missing;
        return;
    };
    let mut cmd = Command::new(&bin);
    cmd.args(["serve", "--port", "0"]).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            d.state = State::Failed(format!("Could not start {}: {e}", bin.display()));
            return;
        }
    };
    let stdout = child.stdout.take();
    if let Some(err) = child.stderr.take() {
        std::thread::spawn(move || {
            for line in BufReader::new(err).lines().map_while(Result::ok) {
                tracing::info!("[cst serve] {line}");
            }
        });
    }
    d.guard = guard(child.id() as i32);
    d.child = Some(child);
    d.state = State::Starting;
    drop(d);
    std::thread::spawn(move || {
        let state = match stdout {
            Some(out) => {
                let mut reader = BufReader::new(out);
                let mut first = String::new();
                let n = reader.read_line(&mut first).unwrap_or(0);
                let state = match parse_discovery(&first) {
                    Some((port, token)) => State::Ready { port, token },
                    None if n == 0 => State::Failed("cst serve exited before it printed its port.".into()),
                    None => State::Failed(format!("cst serve printed something that is not its discovery line: {}", first.trim())),
                };
                // Keep draining, so the daemon never blocks on a full pipe.
                std::thread::spawn(move || {
                    let _ = reader.read_to_end(&mut Vec::new());
                });
                state
            }
            None => State::Failed("cst serve has no stdout.".into()),
        };
        DAEMON.lock().unwrap().state = state;
        ready();
    });
}

/// Stop the daemon and every job it started (its process group).
pub fn stop() {
    let mut d = DAEMON.lock().unwrap();
    if let Some(mut g) = d.guard.take() {
        let _ = g.kill();
        let _ = g.wait();
    }
    if let Some(mut c) = d.child.take() {
        let pg = -(c.id() as i32);
        unsafe { libc::kill(pg, libc::SIGTERM) };
        let until = std::time::Instant::now() + Duration::from_secs(2);
        while std::time::Instant::now() < until && !matches!(c.try_wait(), Ok(Some(_))) {
            std::thread::sleep(Duration::from_millis(50));
        }
        unsafe { libc::kill(pg, libc::SIGKILL) };
        let _ = c.wait();
    }
    d.state = State::Idle;
}

// Ignores the signals aimed at the app's group, waits for end of file on stdin
// (the app is gone), then stops the daemon's group. argv[0] names it.
const GUARD_NAME: &str = "captain-app-cst-guard";
const GUARD_SCRIPT: &str = r#"trap '' TERM INT HUP; while IFS= read -r _; do :; done; kill -TERM -- "-$1" 2>/dev/null; sleep 2; kill -KILL -- "-$1" 2>/dev/null; exit 0"#;

fn guard(pgid: i32) -> Option<Child> {
    use std::os::unix::process::CommandExt;
    Command::new("/bin/sh")
        .arg0(GUARD_NAME)
        .args(["-c", GUARD_SCRIPT, GUARD_NAME, &pgid.to_string()])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .ok()
}

/// What a webview URL resolves to right now.
#[derive(Debug, PartialEq)]
pub enum Url {
    Ready(String),
    /// Shown in the block while waiting.
    Waiting(String),
    Failed(String),
}

/// Fill `{cst}` and `{cst_token}` in a webview URL from `state`.
pub fn fill(url: &str, state: &State) -> Url {
    if !url.contains("{cst}") && !url.contains("{cst_token}") {
        return Url::Ready(url.to_owned());
    }
    match state {
        State::Ready { port, token } => Url::Ready(url.replace("{cst}", &format!("http://127.0.0.1:{port}")).replace("{cst_token}", token)),
        State::Idle | State::Starting => Url::Waiting("Starting Captain Search (cst serve)...".into()),
        State::Missing => Url::Failed(format!("Captain Search is not installed. Install it with: {INSTALL}")),
        State::Failed(e) => Url::Failed(e.clone()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_discovery_line() {
        assert_eq!(parse_discovery(r#"{"port": 51234, "token": "tok", "pid": 9}"#), Some((51234, "tok".into())));
        assert_eq!(parse_discovery("no config found"), None);
        assert_eq!(parse_discovery(r#"{"port": 0, "token": "t"}"#), None);
        assert_eq!(parse_discovery(r#"{"port": 70000, "token": "t"}"#), None);
    }

    #[test]
    fn reads_serve_json() {
        assert_eq!(parse_serve_json(r#"{"port": 4100, "token": "t", "pid": 77, "ui": true}"#), Some((4100, "t".into(), 77)));
        assert_eq!(parse_serve_json(r#"{"port": 4100, "token": "t"}"#), None);
        assert_eq!(parse_serve_json("{"), None);
    }

    #[test]
    fn fills_the_daemon_into_urls() {
        let ready = State::Ready { port: 4100, token: "abc".into() };
        assert_eq!(fill("{cst}/ui/explore?embed=1#token={cst_token}", &ready), Url::Ready("http://127.0.0.1:4100/ui/explore?embed=1#token=abc".into()));
        assert_eq!(fill("http://localhost:3000/", &State::Idle), Url::Ready("http://localhost:3000/".into()));
        assert!(matches!(fill("{cst}/ui", &State::Starting), Url::Waiting(_)));
        assert!(matches!(fill("{cst}/ui", &State::Missing), Url::Failed(m) if m.contains(INSTALL)));
    }
}
