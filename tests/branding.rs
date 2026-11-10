//! CAPTAIN: no Herdr branding anywhere a person can see it.
//!
//! Scans what Captain ships and shows: every shipped mod, frame screen, layout,
//! theme and doc under defaults/ (as embedded by build.rs, so not the
//! NOT_SHIPPED folders), the agent skill, and the user-facing strings in src/.
//! Any /herdr/i left after removing the allowed spots fails the test, with the
//! file and line.
//!
//! Allowed, because they are attribution or identifiers people don't read as
//! the product's name (see FORK.md):
//! - the notice: "a modified version of Herdr (https://github.com/herdrdev/herdr-app)";
//! - the mod module `herdr` (`from "herdr"`, `herdr.d.ts`): renaming it breaks every mod;
//! - `herdr-core`, the terminal engine's real binary and package name, which a person
//!   has to type to install or find it, and its install URL on the setup screen;
//! - `herdr.<label>` terminal labels shared with the core, and `HERDR_*` variables;
//! - GPUI key-context names (`"Herdr"`, `"Herdr layout = {}"`), which never render.

use std::path::{Path, PathBuf};

const ALLOWED: &[&str] = &[
    r"modified version of Herdr \(https://github\.com/herdrdev/herdr-app\)",
    r#"from "herdr""#,
    r"from 'herdr'",
    r"herdr\.d\.ts",
    r"`herdr`",
    r"The `herdr` module",
    r"herdr-core",
    r"Herdr Core\.app",
    r"herdr\.(space|tab|pinned|right-click)",
    r"HERDR_[A-Z0-9_]+",
    r"dev\.herdr\.core",
];

/// Folders build.rs doesn't ship (keep in step with NOT_SHIPPED there).
const NOT_SHIPPED: &[&str] = &["defaults/mods/win98", "defaults/layouts/win98.json", "defaults/layouts/win98.svg"];

/// The scope model's files, where "engine" means the customer's search or recommendation system (see
/// no_engine_or_daemon_a_person_sees).
const SCOPE_MODEL: &[&str] = &["defaults/captain/kit/scopes.js", "defaults/captain/kit/scopemodel.js", "defaults/mods/captain-scope", "defaults/mods/captain-nav/explorer.js", "defaults/mods/captain-engine"];

/// Files on their way out, each with the commit that removes it. Keep this empty.
const PENDING: &[&str] = &[];

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() { walk(&p, out) } else { out.push(p) }
    }
}

fn rel(p: &Path) -> String {
    p.strip_prefix(root()).unwrap().to_string_lossy().replace('\\', "/")
}

/// Text a person can read in this file: JS without comments, Rust string
/// literals outside tests, everything in Markdown, JSON and .d.ts files.
fn visible(path: &str, text: &str) -> Vec<(usize, String)> {
    let mut out = Vec::new();
    if path.ends_with(".rs") {
        let lit = fancy_regex::Regex::new(r#""(?:[^"\\]|\\.)*""#).unwrap();
        for (i, line) in text.lines().enumerate() {
            if line.trim_start().starts_with("#[cfg(test)]") {
                break;
            }
            let t = line.trim_start();
            // Comments and log lines are for developers, not people.
            if t.starts_with("//") || line.contains("tracing::") {
                continue;
            }
            for m in lit.find_iter(line).flatten() {
                let s = m.as_str();
                if s == "\"Herdr\"" || s.starts_with("\"Herdr layout = ") {
                    continue;
                }
                out.push((i + 1, s.to_owned()));
            }
        }
        return out;
    }
    let js = path.ends_with(".js") || path.ends_with(".ts") && !path.ends_with(".d.ts");
    let mut in_block = false;
    for (i, line) in text.lines().enumerate() {
        let mut l = line.to_owned();
        if js {
            if in_block {
                match l.find("*/") { Some(e) => { l = l[e + 2..].to_owned(); in_block = false } None => continue }
            }
            if let Some(s) = l.find("/*") {
                match l[s..].find("*/") { Some(e) => l.replace_range(s..s + e + 2, ""), None => { l.truncate(s); in_block = true } }
            }
            if l.trim_start().starts_with("//") {
                continue;
            }
            // A trailing comment after code (not inside a string like "https://").
            if let Some(c) = l.find(" // ") { l.truncate(c) }
        }
        out.push((i + 1, l));
    }
    out
}

fn hits(path: &str, text: &str) -> Vec<String> {
    let allowed: Vec<fancy_regex::Regex> = ALLOWED.iter().map(|a| fancy_regex::Regex::new(a).unwrap()).collect();
    // In Rust only the product word, the site and the CLI's message prefix can
    // reach a person; lowercase herdr-* there are temp names and protocol markers.
    let herdr = fancy_regex::Regex::new(if path.ends_with(".rs") { r"Herdr|herdr\.dev|herdr-app:" } else { r"(?i)herdr" }).unwrap();
    let mut out = Vec::new();
    for (n, mut line) in visible(path, text) {
        for a in &allowed {
            line = a.replace_all(&line, "").into_owned();
        }
        if path == "src/setup.rs" {
            line = line.replace("releases.herdr.dev/core/install", "");
        }
        // Matching release-note URLs, never fetched (brand::releases is unset).
        if path == "src/update_notes.rs" {
            line = line.replace("https://media.herdr.dev/", "");
        }
        if herdr.is_match(&line).unwrap() {
            out.push(format!("{path}:{n}: {}", line.trim()));
        }
    }
    out
}

#[test]
fn nothing_a_person_sees_says_herdr() {
    let mut files = Vec::new();
    walk(&root().join("defaults"), &mut files);
    walk(&root().join("src"), &mut files);
    // What's New shows these notes.
    files.push(root().join("CHANGELOG-captain.md"));
    let mut found = Vec::new();
    for f in files {
        let r = rel(&f);
        if NOT_SHIPPED.iter().any(|n| r.starts_with(n)) || PENDING.contains(&r.as_str()) {
            continue;
        }
        let shown = r.starts_with("defaults/") || r == "src/SKILL.md" || r == "CHANGELOG-captain.md" || r.ends_with(".rs");
        if !shown || r.contains("_tests.rs") || r.ends_with("tests.rs") {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&f) else { continue };
        found.extend(hits(&r, &text));
    }
    assert!(found.is_empty(), "Herdr branding a person can see ({}):\n{}", found.len(), found.join("\n"));
}

#[test]
fn no_sheep_ships() {
    let mut files = Vec::new();
    walk(&root().join("defaults"), &mut files);
    for f in &files {
        let r = rel(f);
        if NOT_SHIPPED.iter().any(|n| r.starts_with(n)) {
            continue;
        }
        let name = f.file_name().unwrap().to_string_lossy().to_lowercase();
        assert!(!name.contains("herdr") || name == "herdr.d.ts", "{r}: a Herdr asset ships");
    }
    // Upstream's release scripts keep their file names; the bytes are the Captain C.
    let same = |a: &str, b: &str| std::fs::read(root().join(a)).unwrap() == std::fs::read(root().join(b)).unwrap();
    assert!(same("assets/icons/Herdr.icns", "assets/icons/Captain.icns"));
    assert!(same("assets/icons/herdr-app.ico", "assets/icons/captain-app.ico"));
    assert!(same("assets/icons/herdr-ui-icon-clean.png", "assets/icons/captain-ui-icon.png"));
    assert!(same("assets/icons/herdr-ui-icon-clean.svg", "assets/icons/captain-ui-icon.svg"));
}

#[test]
fn the_scanner_catches_branding_and_lets_identifiers_through() {
    assert_eq!(hits("defaults/mods/x/main.js", "div().child(\"Welcome to Herdr\")").len(), 1);
    assert!(hits("defaults/mods/x/main.js", "// Herdr draws this\nimport { run } from \"herdr\";").is_empty());
    assert!(hits("defaults/AGENTS.md", "Captain is a modified version of Herdr (https://github.com/herdrdev/herdr-app), Apache 2.0.").is_empty());
    assert_eq!(hits("defaults/AGENTS.md", "share at herdr.dev/mods").len(), 1);
    assert!(hits("src/x.rs", "let c = key_context(\"Herdr\");\n// Herdr\n").is_empty());
    assert_eq!(hits("src/x.rs", "fail(\"no Herdr window\")").len(), 1);
    assert!(hits("src/x.rs", "#[cfg(test)]\nfn t() { \"Herdr\"; \"in Herdr\"; }").is_empty());
}

/// Edgar: "what is this engine? ... why is there an engine?" Captain's backend
/// is never a concept a person sees: no visible "engine", "daemon" or "core". Checked in
/// what people read: string literals in mods and Rust, and the docs the app
/// writes. Identifiers (`captain.daemon`, the shared key mods read) and Codex's
/// literal `--no-daemon` flag aren't prose.
fn backend_words(path: &str, text: &str) -> Vec<String> {
    // "core" too, where it means hc's terminal runtime ("reconnecting to core").
    let word = fancy_regex::Regex::new(r"(?i)\b(engine|daemon|core)s?\b").unwrap();
    // A bare lowercase "engine" is a key (`n.kind === "engine"`), not prose a person reads.
    let allowed = fancy_regex::Regex::new(r#"^["'`]engines?["'`]$|captain\.daemon(\.retry)?|--no-daemon|herdr-core|`[^`]*`|^\W*core\W*$|dev\.herdr\.core|Herdr Core\.app|core/|[\w]+-core[\w-]*|core-[\w-]+"#).unwrap();
    let lit = fancy_regex::Regex::new(r#""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`"#).unwrap();
    let mut out = Vec::new();
    let prose = path.ends_with(".md");
    for (n, line) in visible(path, text) {
        // Log lines are for developers, not people.
        if line.contains("tracing::") {
            continue;
        }
        let parts: Vec<String> = if prose { vec![line.clone()] } else { lit.find_iter(&line).flatten().map(|m| m.as_str().to_owned()).collect() };
        for p in parts {
            let p = allowed.replace_all(&p, "").into_owned();
            if word.is_match(&p).unwrap() {
                out.push(format!("{path}:{n}: {}", p.trim()));
            }
        }
    }
    out
}

#[test]
fn no_engine_or_daemon_a_person_sees() {
    let mut files = Vec::new();
    walk(&root().join("defaults"), &mut files);
    walk(&root().join("src"), &mut files);
    let mut found = Vec::new();
    for f in files {
        let r = rel(&f);
        if NOT_SHIPPED.iter().any(|n| r.starts_with(n)) || r.contains("/fonts/") || r.ends_with(".d.ts") {
            continue;
        }
        // Captain's own surfaces: its mods and frame screens, the docs the app writes, and Rust strings.
        let captain_js = r.starts_with("defaults/mods/captain-") || r.starts_with("defaults/frame/") || r.starts_with("defaults/captain/kit/");
        let doc = r == "defaults/AGENTS.md" || r == "src/SKILL.md";
        let rust = r.ends_with(".rs") && !r.contains("tests.rs") && !r.contains("_tests");
        if !(captain_js && r.ends_with(".js") || doc || rust) {
            continue;
        }
        // Edgar's product spec (2026-11-06) names the customer's search or recommendation system an Engine
        // (Brand > Site > Engine, "All engines", "Add engine"). That's a different thing from Captain's own
        // backend, which stays unnamed: the scope model's files may say "engine" in that sense, nowhere else.
        if SCOPE_MODEL.iter().any(|m| r.starts_with(m) || r.ends_with("/kit/scopes.js") || r.ends_with("/kit/scopemodel.js")) {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&f) else { continue };
        found.extend(backend_words(&r, &text));
    }
    assert!(found.is_empty(), "\"engine\", \"daemon\" or \"core\" a person can see ({}):\n{}", found.len(), found.join("\n"));
}

#[test]
fn the_backend_word_check_reads_only_prose() {
    assert_eq!(backend_words("defaults/mods/captain-x/main.js", r#"div().child("Starting Captain's engine")"#).len(), 1);
    assert!(backend_words("defaults/mods/captain-x/main.js", "const d = shared(\"captain.daemon\"); // the daemon").is_empty());
    assert!(backend_words("defaults/AGENTS.md", "Codex starts as `codex --no-daemon`.").is_empty());
}
