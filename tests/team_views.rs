//! CAPTAIN: `captain-app check` validates a team view (captain/views/<name>/) with the Captain kit injected, the way
//! the app loads it. The template `cst view new` scaffolds (backend/core/captain_search/view_template) passes; a broken view and
//! a view needing a newer view API fail, saying why.

use std::path::{Path, PathBuf};
use std::process::Command;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// The template filled in for `name`, in a fresh project folder.
fn scaffold(name: &str) -> (PathBuf, PathBuf) {
    let project = std::env::temp_dir().join(format!("captain-team-view-test-{}-{name}", std::process::id()));
    let view = project.join("captain").join("views").join(name);
    let _ = std::fs::remove_dir_all(&project);
    std::fs::create_dir_all(&view).unwrap();
    std::fs::write(project.join("captain").join("captain.toml"), "").unwrap();
    for f in ["mod.json", "main.js", "README.md", ".gitignore"] {
        let text = std::fs::read_to_string(root().join("backend/core/captain_search/view_template").join(f)).unwrap();
        std::fs::write(view.join(f), text.replace("{{name}}", name).replace("{{title}}", "Catalog rows")).unwrap();
    }
    (project, view)
}

fn check(view: &Path) -> (i32, String) {
    let home = std::env::temp_dir().join(format!("captain-team-view-home-{}", std::process::id()));
    let out = Command::new(env!("CARGO_BIN_EXE_captain-app")).arg("check").arg(view).env("CAPTAIN_APP_HOME", &home).output().unwrap();
    (out.status.code().unwrap_or(-1), format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr)))
}

#[test]
fn the_template_passes_and_broken_views_say_why() {
    let (project, view) = scaffold("rows");
    let (code, out) = check(&view);
    assert_eq!(code, 0, "the template should pass: {out}");
    assert!(out.contains("view API 1"), "it says which view API: {out}");
    assert!(!view.join("kit").exists(), "checking never touches the author's folder");

    // A view that breaks.
    let main = std::fs::read_to_string(view.join("main.js")).unwrap();
    std::fs::write(view.join("main.js"), main.replace("render() {", "render() { this.oops();")).unwrap();
    let (code, out) = check(&view);
    assert_ne!(code, 0, "a broken view fails: {out}");
    let real = std::fs::canonicalize(&view).unwrap();
    assert!(out.contains(&format!("{}/main.js", real.display())), "and names the author's file: {out}");
    std::fs::write(view.join("main.js"), main).unwrap();

    // A view written for a newer Captain.
    let manifest = std::fs::read_to_string(view.join("mod.json")).unwrap();
    std::fs::write(view.join("mod.json"), manifest.replace("\"view_api\": 1", "\"view_api\": 99")).unwrap();
    let (code, out) = check(&view);
    assert_ne!(code, 0, "a newer view API fails: {out}");
    assert!(out.contains("Update Captain"), "and says what to do: {out}");
    let _ = std::fs::remove_dir_all(project);
}
