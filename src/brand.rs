//! CAPTAIN: who this build is. Captain is a modified version of Herdr
//! (https://github.com/herdrdev/herdr-app, Apache 2.0). Apache 2.0 grants no
//! trademark rights, so every name, id, path and endpoint that says who the
//! app is comes from here, and upstream code points at these instead of
//! spelling "Herdr". See FORK.md.
//!
//! Captain runs side by side with a real Herdr: its own home
//! (`~/.config/captain-app`), its own control socket, its own app id and its
//! own herdr-core session (`captain`), so the two never share spaces, tabs or
//! terminals. It never contacts herdr.dev: no telemetry, no feedback upload,
//! no app or core updates, no release-note media.

/// The product name people see: window title, menus, setup screen.
pub const NAME: &str = "Captain";
/// The executable and the CLI (`captain-app do ...`).
pub const BIN: &str = "captain-app";
/// macOS bundle identifier and the Linux/Windows app id.
pub const BUNDLE_ID: &str = "dev.captain.app";
/// The app bundle's folder name and the executable inside it.
pub const BUNDLE: &str = "Captain.app";
pub const BUNDLE_EXE: &str = "Captain";
/// The home folder's name under ~/.config (or $XDG_CONFIG_HOME).
pub const HOME_DIR: &str = "captain-app";
/// Picks another home (tests, experiments). Upstream's HERDR_APP_HOME is
/// deliberately not read: a shell set up for Herdr must never point Captain
/// at Herdr's home.
pub const HOME_ENV: &str = "CAPTAIN_APP_HOME";
/// The control socket in the home (`captain-app do`, `status`, `shot`).
pub const SOCKET: &str = "captain-app.sock";
/// The herdr-core session Captain's core runs in. Herdr uses "default".
pub const CORE_SESSION: &str = "captain";
/// Picks another core session for Captain.
pub const CORE_SESSION_ENV: &str = "CAPTAIN_CORE_SESSION";
/// The layout a new home opens with, and where the escape hatch goes back to.
pub const DEFAULT_LAYOUT: &str = "captain";
/// Where the source and the issue tracker live.
pub const REPO: &str = "https://github.com/runcaptain/captain-app";
/// What Captain is, in Edgar's words: the About blurb, the onboarding's first line and AGENTS.md's.
pub const ABOUT: &str = "Captain is the open-source platform for building better search and recommendation models: from signal discovery to reproducible experiments and measured production improvements.";
/// Shown in the About panel.
pub const NOTICE: &str = "Captain is a modified version of Herdr (https://github.com/herdrdev/herdr-app), Apache 2.0.";

/// The core session to use. CAPTAIN_CORE_SESSION wins. A test session
/// (HC_SESSION=test-..., as upstream's tests and sandbox set) is kept. Anything
/// else, including Herdr's own "default" exported into a Herdr terminal, is
/// replaced by Captain's session.
pub fn core_session() -> String {
    let get = |k: &str| std::env::var(k).ok().map(|v| v.trim().to_owned()).filter(|v| !v.is_empty());
    pick_session(get(CORE_SESSION_ENV), get("HC_SESSION"))
}

fn pick_session(captain: Option<String>, hc: Option<String>) -> String {
    match (captain, hc) {
        (Some(s), _) => s,
        (None, Some(s)) if s.starts_with("test-") => s,
        _ => CORE_SESSION.to_owned(),
    }
}

/// Put Captain's core session in this process's environment, so every `hc`
/// the app starts (connect, version checks, the setup screen) reaches it.
/// Call first thing in main, before any thread starts.
pub fn pin_core_session() {
    let session = core_session();
    // SAFETY: called at the top of main, while the process has one thread.
    unsafe { std::env::set_var("HC_SESSION", session) };
}

/// Captain's own release server, if this build was pointed at one
/// (CAPTAIN_RELEASES_URL). None: no app updates. Captain never checks or
/// downloads herdr.dev releases.
pub fn releases() -> Option<String> {
    std::env::var("CAPTAIN_RELEASES_URL").ok().filter(|u| !u.trim().is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_fork_never_names_herdr_dev() {
        for s in [NAME, BIN, BUNDLE_ID, BUNDLE, HOME_DIR, SOCKET, CORE_SESSION, REPO] {
            assert!(!s.to_lowercase().contains("herdr"), "{s}");
        }
        assert_ne!(CORE_SESSION, "default");
    }

    #[test]
    fn captain_never_joins_herdrs_core_session() {
        let s = |v: &str| Some(v.to_owned());
        assert_eq!(pick_session(None, None), "captain");
        // Started from a terminal inside Herdr, which exports Herdr's session.
        assert_eq!(pick_session(None, s("default")), "captain");
        assert_eq!(pick_session(None, s("work")), "captain");
        assert_eq!(pick_session(None, s("test-abc")), "test-abc");
        assert_eq!(pick_session(s("mine"), s("default")), "mine");
    }
}
