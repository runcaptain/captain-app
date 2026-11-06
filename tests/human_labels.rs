//! CAPTAIN: the UI speaks in tasks and plain words; the CLI is for agents (Edgar: "we
//! should probably not direct the UI to be a cli wrapper"). Fails if a menu or palette
//! entry a person sees reads like a cst command ("cloud add", "data / class add",
//! "cst demo seed"). Raw commands live behind one "Run a cst command..." entry.

use std::path::Path;

/// cst's command groups (`cst --help`).
const GROUPS: &[&str] = &[
    "init", "sync", "index", "sql", "serve", "lineage", "code", "workspace", "secret", "conn", "data", "queries", "judge",
    "pipeline", "sim", "backtest", "deploy", "notebook", "skill", "demo", "cloud", "compute", "models", "workflow",
    "investigate", "signal", "handoff", "propose", "replay", "grade", "labels", "runs", "prompt", "jobs",
];

fn raw(label: &str) -> bool {
    if label.starts_with("Run a cst command") {
        return false;
    }
    let l = label.trim();
    if l.starts_with("cst ") || l.contains(" / ") {
        return true;
    }
    let first = l.split(|c: char| c == ' ' || c == '/').next().unwrap_or("");
    GROUPS.contains(&first) && l.len() > first.len()
}

/// `title: "..."` and `label: "..."` literals: what menus, tasks and palette entries show.
fn labels(text: &str) -> Vec<String> {
    let re = fancy_regex::Regex::new(r#"\b(?:title|label)\s*:\s*"([^"]*)""#).unwrap();
    re.captures_iter(text).flatten().filter_map(|c| c.get(1).map(|m| m.as_str().to_owned())).collect()
}

#[test]
fn no_menu_or_palette_entry_reads_like_a_command() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let mut files = vec![root.join("defaults/frame/palette/main.js"), root.join("defaults/frame/welcome/main.js")];
    for e in std::fs::read_dir(root.join("defaults/mods")).unwrap().flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        // captain-commands is the power-user list behind "Run a cst command...": raw by design.
        if name.starts_with("captain-") && name != "captain-commands" {
            for f in ["main.js", "tasks.js"] {
                if e.path().join(f).exists() {
                    files.push(e.path().join(f));
                }
            }
        }
    }
    let mut found = Vec::new();
    for f in files {
        for l in labels(&std::fs::read_to_string(&f).unwrap()) {
            if raw(&l) {
                found.push(format!("{}: {l:?}", f.strip_prefix(root).unwrap().display()));
            }
        }
    }
    assert!(found.is_empty(), "labels that read like cst commands:\n{}", found.join("\n"));
}

#[test]
fn the_check_tells_commands_from_words() {
    for bad in ["cloud add", "data / class add", "cst demo seed", "compute add", "demo seed"] {
        assert!(raw(bad), "{bad}");
    }
    for good in ["Data and signals", "Connect a cloud account", "New investigation", "Run a cst command...", "Hand off a candidate", "Open makro-catalog"] {
        assert!(!raw(good), "{good}");
    }
}
