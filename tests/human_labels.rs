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
    for good in ["Data and signals", "Connect a cloud account", "New experiment", "Run a cst command...", "Start a launch review", "Open gift-catalog"] {
        assert!(!raw(good), "{good}");
    }
}

// ---- screen words -------------------------------------------------------------------------------------------------
// CAPTAIN: the screens speak the words search and recommendation teams use (Edgar, 2026-11-10; the research is
// "How search and recommendation teams are organized, what their tools call things"). The earlier words stay in
// the CLI, the API, ids and file names, where agents and saved workspaces depend on them, and as palette search
// words, so typing "backtest" still finds "Run an offline evaluation". defaults/AGENTS.md has the table ("Words").
// This fails when a retired word comes back in text a person reads.

/// (what was written, what the screen says now). Matched without case, on whole words.
const RETIRED: &[(&str, &str)] = &[
    (r"\bbacktest(s|ed|ing)?\b", "offline evaluation"),
    (r"\binvestigations?\b", "experiment"),
    (r"\bchampions?\b", "baseline"),
    (r"\bhand(ed|ing|s)?[ -]?offs?\b|\bhand (it|them) off\b", "launch review"),
    (r"\bholdouts?\b", "sealed test set"),
    (r"\bgrading\b|\bgraders?\b", "rating, rater"),
    (r"(?<!training )(?<!ranking )\bpipelines?\b", "model version, or search configuration"),
    (r"\b(a|no|the|run|this|pick a) replays?\b|^replays?$", "off-policy evaluation"),
    (r"^comparisons$|\b(in|to|open) Comparisons\b", "Compare"),
    (r"\breadiness checks?\b", "launch criteria"),
];

/// The parts of a line that only look like prose: palette and icon search words (which keep the earlier words on
/// purpose), a command an agent prompt names, an environment variable, and the one sentence that tells MLflow
/// users where their "champion" alias went.
fn not_shown(line: &str) -> String {
    let mut l = line.to_owned();
    for re in [
        r#"\b(words|was)\s*:\s*("(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`)"#,
        // A New menu or palette row: [task, title, search words, icon].
        r#"(\["[\w-]+",\s*"[^"]*",\s*)"[^"]*"(,\s*"[\w-]+"\])"#,
        r"\(cst [a-z-]+\)",
        r"CST_[A-Z_]+",
        r#"\\"champion\\" alias|`champion` alias"#,
    ] {
        let re = fancy_regex::Regex::new(re).unwrap();
        l = if re.as_str().starts_with(r#"(\["#) { re.replace_all(&l, "$1\"\"$2").into_owned() } else { re.replace_all(&l, "").into_owned() };
    }
    l
}

/// String literals a person reads in a mod's source: sentences and labels, never ids, object keys, paths or
/// comments. `${...}` holds code, so it is dropped first.
fn shown(text: &str) -> Vec<(usize, String)> {
    let lit = fancy_regex::Regex::new(r#""(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`"#).unwrap();
    let hole = fancy_regex::Regex::new(r"\$\{[^}]*\}").unwrap();
    let label = fancy_regex::Regex::new(r"^[A-Z][A-Za-z]*$").unwrap();
    let badge = fancy_regex::Regex::new(r#"Badge\(c, "([a-z]+)""#).unwrap();
    let mut out = Vec::new();
    let mut in_block = false;
    for (i, raw) in text.lines().enumerate() {
        let mut l = raw.to_owned();
        if in_block {
            match l.find("*/") { Some(e) => { l = l[e + 2..].to_owned(); in_block = false } None => continue }
        }
        if let Some(s) = l.find("/*") {
            match l[s..].find("*/") { Some(e) => l.replace_range(s..s + e + 2, ""), None => { l.truncate(s); in_block = true } }
        }
        if l.trim_start().starts_with("//") {
            continue;
        }
        if let Some(c) = l.find(" // ") { l.truncate(c) }
        let l = hole.replace_all(&not_shown(&l), "").into_owned();
        for m in lit.find_iter(&l).flatten() {
            // An object key ("backtest run": ...) is looked up, not shown.
            if l[m.end()..].trim_start().starts_with(':') {
                continue;
            }
            let s = &m.as_str()[1..m.as_str().len() - 1];
            // A path or an id has no space; a one-word label starts with a capital.
            if s.trim().contains(' ') && !s.starts_with('/') || label.is_match(s).unwrap() {
                out.push((i + 1, s.to_owned()));
            }
        }
        for c in badge.captures_iter(&l).flatten() {
            out.push((i + 1, c.get(1).unwrap().as_str().to_owned()));
        }
    }
    out
}

fn retired(s: &str) -> Option<&'static str> {
    RETIRED.iter().find(|(re, _)| fancy_regex::Regex::new(&format!("(?i){re}")).unwrap().is_match(s).unwrap()).map(|(_, now)| *now)
}

#[test]
fn no_retired_screen_word_comes_back() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let mut files = Vec::new();
    let js = |dir: std::path::PathBuf, files: &mut Vec<std::path::PathBuf>| {
        for e in std::fs::read_dir(dir).unwrap().flatten() {
            if e.path().extension().is_some_and(|x| x == "js") {
                files.push(e.path());
            }
        }
    };
    // The kit once (each mod's ./kit is a copy), Captain's own screens, and the app's welcome and palette.
    js(root.join("defaults/captain/kit"), &mut files);
    js(root.join("defaults/frame/welcome"), &mut files);
    js(root.join("defaults/frame/palette"), &mut files);
    for e in std::fs::read_dir(root.join("defaults/mods")).unwrap().flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        // captain-commands lists raw cst commands by design; captain-project-icon's strings are icon search words.
        if name.starts_with("captain-") && name != "captain-commands" && name != "captain-project-icon" {
            js(e.path(), &mut files);
            files.push(e.path().join("mod.json"));
        }
    }
    let mut found = Vec::new();
    for f in files {
        let Ok(text) = std::fs::read_to_string(&f) else { continue };
        for (n, s) in shown(&text) {
            if let Some(now) = retired(&s) {
                found.push(format!("{}:{n}: {s:?} (the screens say: {now})", f.strip_prefix(root).unwrap().display()));
            }
        }
    }
    // A project recipe's own words reach the New project form as they are written.
    for e in std::fs::read_dir(root.join("backend/core/captain_search/recipes")).unwrap().flatten() {
        let text = std::fs::read_to_string(e.path()).unwrap();
        for (n, line) in text.lines().enumerate() {
            let prose = line.trim_start();
            if prose.starts_with("id:") || prose.starts_with("method:") || prose.starts_with("holdout:") {
                continue;
            }
            // `role: holdout` and `id: holdout` are the slot's key, not its label.
            let prose = fancy_regex::Regex::new(r"\b(id|role|method): [\w-]+").unwrap().replace_all(prose, "").into_owned();
            if let Some(now) = retired(&prose) {
                found.push(format!("{}:{}: {prose:?} (the screens say: {now})", e.path().strip_prefix(root).unwrap().display(), n + 1));
            }
        }
    }
    assert!(found.is_empty(), "retired screen words a person can read ({}):\n{}", found.len(), found.join("\n"));
}

#[test]
fn the_screen_word_check_reads_what_people_read() {
    let hit = |src: &str| shown(src).into_iter().filter(|(_, s)| retired(s).is_some()).count();
    // Labels, sentences and badges are read.
    assert_eq!(hit(r#"Button(c, "x", "Run a backtest")"#), 1);
    assert_eq!(hit(r#"const TABS = [["backtests", "Backtests"], ["grading", "Grading"]];"#), 2);
    assert_eq!(hit(r#"Empty(c, "No handoffs yet", "A handoff passes a pipeline version on.")"#), 2);
    assert_eq!(hit(r#"x.child(Badge(c, "champion", "blue"))"#), 1);
    assert_eq!(hit(r#"div().child("Sealed holdout")"#), 1);
    assert_eq!(hit(r#"div().child("Pick a replay.")"#), 1);
    // Ids, paths, object keys, comments, search words and named commands aren't.
    assert_eq!(hit(r#"api(`/api/backtests/${enc(name)}/run`); go("comparisons", { backtest: b.name });"#), 0);
    assert_eq!(hit(r#"const V = { "backtest run": "Run offline evaluation", "handoff create": "Send to launch review" };"#), 0);
    assert_eq!(hit("// Backtests and their runs\nconst a = 1; // the champion"), 0);
    assert_eq!(hit(r#"["backtest", "Run an offline evaluation", "backtest compare evaluate", "flask-conical"],"#), 0);
    assert_eq!(hit(r#"{ title: `Open ${b.name}`, words: "offline evaluation backtest run" }"#), 0);
    assert_eq!(hit(r#"`look at the offline evaluation ${name} (cst backtest): it compares`"#), 0);
    // The new words pass, and so do the phrases where the old ones are meant literally.
    for good in ["Run an offline evaluation", "Start a launch review", "Bring your own training pipeline", "Replay logged searches",
        "Off-policy evaluation (counterfactual replay of logged traffic)", "Every comparison", "Sealed test set", "Rating"] {
        assert!(retired(good).is_none(), "{good}");
    }
}
