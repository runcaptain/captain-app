//! CAPTAIN: the Captain Search backend (`cst serve`) behind Captain's native screens.
//!
//! At launch Captain starts `cst serve --port 0` (or reuses a new-enough one),
//! reads its first stdout line (`{"port", "token", "pid"}`) and tells the mods where
//! it is through shared "captain.daemon". The daemon runs in its own process group and is
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

/// Not `uv tool install captain-search`: that PyPI name is an unrelated package.
pub const INSTALL: &str = "git clone https://github.com/runcaptain/captain-app.git && captain-app/backend/install.sh";

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

/// $CST_BIN, then settings.json `cst` (for development), then the cst packaged inside
/// Captain.app. Only a build without one (run from target/) looks in ~/.local/bin and PATH.
fn find() -> Option<PathBuf> {
    if let Some(b) = std::env::var_os("CST_BIN").map(PathBuf::from).filter(|b| is_executable(b)) {
        return Some(b);
    }
    if let Some(b) = crate::settings::load_person().ok().and_then(|p| p.cst).filter(|c| !c.is_empty()) {
        let b = match b.strip_prefix("~/") { Some(rest) => std::env::var_os("HOME").map(|h| PathBuf::from(h).join(rest)).unwrap_or_else(|| PathBuf::from(&b)), None => PathBuf::from(&b) };
        if is_executable(&b) {
            return Some(b);
        }
    }
    if let Some(b) = crate::brand::bundled("cst/bin/cst").filter(|b| is_executable(b)) {
        return Some(b);
    }
    let local = std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/bin/cst"));
    let on_path = std::env::var_os("PATH").into_iter().flat_map(|p| std::env::split_paths(&p).collect::<Vec<_>>()).map(|d| d.join("cst"));
    local.into_iter().chain(on_path).find(|c| is_executable(c))
}

/// Offer the packaged cst on the command line as ~/.local/bin/cst, so agents in
/// terminals use the same version. Only when there's no cst there yet, or the link
/// already points into a Captain.app: a cst the person installed is never replaced.
pub fn link_cli() {
    let (Some(target), Some(home)) = (crate::brand::bundled("cst/bin/cst"), std::env::var_os("HOME")) else { return };
    let link = PathBuf::from(home).join(".local/bin/cst");
    if let Err(e) = link_cli_at(&link, &target) {
        tracing::warn!("cannot link {}: {e}", link.display());
    }
}

pub fn link_cli_at(link: &Path, target: &Path) -> std::io::Result<()> {
    match std::fs::symlink_metadata(link) {
        Ok(m) if m.file_type().is_symlink() => {
            let now = std::fs::read_link(link)?;
            if now == target {
                return Ok(());
            }
            if !now.components().any(|c| c.as_os_str().to_string_lossy().ends_with(".app")) || !now.to_string_lossy().contains("/Contents/Resources/cst/") {
                return Ok(()); // someone else's cst
            }
            std::fs::remove_file(link)?;
        }
        Ok(_) => return Ok(()), // a real file: someone else's
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    std::fs::create_dir_all(link.parent().unwrap())?;
    std::os::unix::fs::symlink(target, link)
}

/// The first stdout line of `cst serve --port 0`.
pub fn parse_discovery(line: &str) -> Option<(u16, String)> {
    let v: Value = serde_json::from_str(line.trim()).ok()?;
    let port = u16::try_from(v.get("port")?.as_u64()?).ok().filter(|p| *p > 0)?;
    let token = v.get("token")?.as_str()?.to_owned();
    Some((port, token))
}

/// Captain Search's home: $CST_HOME, else ~/.config/captain-search. Workspaces' agent
/// folders live in <home>/agent/<workspace>.
pub fn cst_home() -> Option<PathBuf> {
    std::env::var_os("CST_HOME").filter(|v| !v.is_empty()).map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config/captain-search")))
}

fn serve_json() -> Option<PathBuf> {
    Some(cst_home()?.join("serve.json"))
}

/// Environment for a terminal in `space`: the Captain context it's bound to
/// (shared "captain.spaces" {<space>: {workspace, investigation?}}, else the
/// current workspace from shared "captain.workspace"), and ~/.local/bin first on
/// PATH so `cst` is found. Agents and Copy prompt read CST_WORKSPACE and
/// CST_INVESTIGATION to know where they are.
pub fn terminal_env(space: &str) -> serde_json::Map<String, Value> {
    let bound = crate::mods::shared("captain.spaces").and_then(|m| m.get(space).cloned()).unwrap_or(Value::Null);
    let workspace = bound.get("workspace").and_then(Value::as_str).map(str::to_owned)
        .or_else(|| crate::mods::shared("captain.workspace").and_then(|v| v.as_str().map(str::to_owned)));
    env_for(workspace.as_deref(), bound.get("investigation").and_then(Value::as_str), std::env::var("PATH").ok().as_deref())
}

pub fn env_for(workspace: Option<&str>, investigation: Option<&str>, path: Option<&str>) -> serde_json::Map<String, Value> {
    let mut env = serde_json::Map::new();
    if let Some(w) = workspace.filter(|w| !w.is_empty()) {
        env.insert("CST_WORKSPACE".into(), Value::String(w.into()));
    }
    if let Some(i) = investigation.filter(|i| !i.is_empty()) {
        env.insert("CST_INVESTIGATION".into(), Value::String(i.into()));
    }
    if let Some(home) = std::env::var_os("HOME") {
        let local = PathBuf::from(home).join(".local/bin").display().to_string();
        let rest = path.unwrap_or("/usr/bin:/bin:/usr/sbin:/sbin");
        if !rest.split(':').any(|p| p == local) {
            env.insert("PATH".into(), Value::String(format!("{local}:{rest}")));
        }
    }
    env
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
    let mut stream = std::net::TcpStream::connect_timeout(&addr, Duration::from_millis(300)).ok()?;
    // An older cst (another app's) lacks what Captain's screens need: per-request
    // workspaces and the cloud summary arrived together. Reuse only one that has them.
    new_enough(&mut stream, &token).then_some((port, token))
}

/// Ask a running cst for a route Captain needs; true on any 2xx.
fn new_enough(stream: &mut std::net::TcpStream, token: &str) -> bool {
    use std::io::Write;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let req = format!("GET {PROBE} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut head = [0u8; 32];
    let n = stream.read(&mut head).unwrap_or(0);
    status_ok(&head[..n])
}

/// The route whose presence means a cst serves what Captain's screens read.
const PROBE: &str = "/api/cloud/summary";

fn status_ok(head: &[u8]) -> bool {
    let line = String::from_utf8_lossy(head);
    line.split_whitespace().nth(1).and_then(|c| c.parse::<u16>().ok()).is_some_and(|c| (200..300).contains(&c))
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

/// What mods read from shared "captain.daemon": `{base, token}` once the daemon
/// is up, else `{state, error?}` (starting, missing with the install command, failed).
pub fn shared_value(state: &State) -> Value {
    match state {
        State::Ready { port, token } => serde_json::json!({ "state": "ready", "base": format!("http://127.0.0.1:{port}"), "token": token, "home": cst_home().map(|h| h.display().to_string()) }),
        State::Idle | State::Starting => serde_json::json!({ "state": "starting" }),
        State::Missing => serde_json::json!({ "state": "missing", "error": format!("Captain Search isn't installed. Install it with: {INSTALL}") }),
        State::Failed(e) => serde_json::json!({ "state": "failed", "error": e }),
    }
}

thread_local! {
    static PUBLISHED: std::cell::RefCell<Option<State>> = const { std::cell::RefCell::new(None) };
    static RETRY: std::cell::RefCell<Option<Value>> = const { std::cell::RefCell::new(None) };
}

/// Each window render (main thread): start or reuse the daemon once, and keep
/// shared "captain.daemon" current for the Captain mods. Native screens reach the
/// daemon over HTTP with it; nothing else needs to know where it is.
pub fn tick() {
    // Tests render windows; they must never start a real daemon. CAPTAIN_NO_DAEMON does the same for sandboxes.
    if cfg!(test) || std::env::var_os("CAPTAIN_NO_DAEMON").is_some() {
        return;
    }
    // A screen's Retry bumps shared "captain.daemon.retry": start over from scratch.
    let retry = crate::mods::shared("captain.daemon.retry");
    let fresh = RETRY.with_borrow_mut(|last| if *last != retry { let changed = last.is_some(); *last = retry; changed } else { false });
    if fresh && matches!(state(), State::Failed(_) | State::Missing) {
        stop();
    }
    if state() == State::Idle {
        ensure(|| crate::mods::request(crate::mods::Request::Refresh));
    }
    let now = state();
    PUBLISHED.with_borrow_mut(|last| {
        if last.as_ref() != Some(&now) {
            crate::mods::set_shared("captain.daemon".into(), shared_value(&now));
            *last = Some(now);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_cli_link_never_replaces_someone_elses_cst() {
        let dir = std::env::temp_dir().join(format!("captain-cst-link-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("bin")).unwrap();
        let ours = dir.join("Captain.app/Contents/Resources/cst/bin/cst");
        let link = dir.join("bin/cst");
        link_cli_at(&link, &ours).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), ours);
        // An older Captain's link is updated.
        let newer = dir.join("Other/Captain.app/Contents/Resources/cst/bin/cst");
        link_cli_at(&link, &newer).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), newer);
        // The person's own cst (install.sh's venv) stays.
        std::fs::remove_file(&link).unwrap();
        let theirs = dir.join("share/captain-search/venv/bin/cst");
        std::os::unix::fs::symlink(&theirs, &link).unwrap();
        link_cli_at(&link, &ours).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), theirs);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn reads_the_discovery_line() {
        assert_eq!(parse_discovery(r#"{"port": 51234, "token": "tok", "pid": 9}"#), Some((51234, "tok".into())));
        assert_eq!(parse_discovery("no config found"), None);
        assert_eq!(parse_discovery(r#"{"port": 0, "token": "t"}"#), None);
        assert_eq!(parse_discovery(r#"{"port": 70000, "token": "t"}"#), None);
    }

    #[test]
    fn terminals_know_their_captain_context() {
        let env = env_for(Some("acme"), Some("01abc"), Some("/usr/bin:/bin"));
        assert_eq!(env["CST_WORKSPACE"], "acme");
        assert_eq!(env["CST_INVESTIGATION"], "01abc");
        assert!(env["PATH"].as_str().unwrap().ends_with("/.local/bin:/usr/bin:/bin"));
        let bare = env_for(None, None, Some("/usr/bin"));
        assert!(!bare.contains_key("CST_WORKSPACE") && !bare.contains_key("CST_INVESTIGATION"));
    }

    #[test]
    fn mods_get_the_daemon_or_why_not() {
        let ready = shared_value(&State::Ready { port: 4100, token: "t".into() });
        assert_eq!(ready["base"], "http://127.0.0.1:4100");
        assert_eq!(ready["token"], "t");
        assert_eq!(shared_value(&State::Starting)["state"], "starting");
        assert!(shared_value(&State::Missing)["error"].as_str().unwrap().contains("install.sh"));
    }

    #[test]
    fn only_a_new_enough_cst_is_reused() {
        assert!(status_ok(b"HTTP/1.1 200 OK\r\n"));
        assert!(!status_ok(b"HTTP/1.1 404 Not Found\r\n"));
        assert!(!status_ok(b""));
    }

    #[test]
    fn reads_serve_json() {
        assert_eq!(parse_serve_json(r#"{"port": 4100, "token": "t", "pid": 77, "ui": true}"#), Some((4100, "t".into(), 77)));
        assert_eq!(parse_serve_json(r#"{"port": 4100, "token": "t"}"#), None);
        assert_eq!(parse_serve_json("{"), None);
    }

}
