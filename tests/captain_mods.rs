//! CAPTAIN: the captain-* mods each carry defaults/captain/cst.js as ./cst.js,
//! because gpui-kit mods can't import outside their folder. Keep them identical.

#[test]
fn every_captain_mod_has_the_same_daemon_client() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let source = std::fs::read_to_string(root.join("defaults/captain/cst.js")).unwrap();
    let mut seen = 0;
    for e in std::fs::read_dir(root.join("defaults/mods")).unwrap().flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if !name.starts_with("captain-") {
            continue;
        }
        let copy = e.path().join("cst.js");
        if copy.exists() || std::fs::read_to_string(e.path().join("main.js")).unwrap_or_default().contains("./cst.js") {
            let text = std::fs::read_to_string(&copy).unwrap_or_else(|_| panic!("{name} imports ./cst.js but has none"));
            assert_eq!(text, source, "defaults/mods/{name}/cst.js differs from defaults/captain/cst.js: copy it again");
            seen += 1;
        }
    }
    // The onboarding (the welcome frame) reads the daemon too.
    assert_eq!(std::fs::read_to_string(root.join("defaults/frame/welcome/cst.js")).unwrap(), source, "defaults/frame/welcome/cst.js differs: copy it again");
    assert!(seen >= 2, "expected the captain mods to carry cst.js");
}
