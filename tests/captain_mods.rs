//! CAPTAIN: the captain-ui kit (defaults/captain/kit) is copied into every Captain mod
//! as ./kit/, because gpui-kit mods can't import outside their folder. Keep the copies
//! identical: edit defaults/captain/kit, then run scripts/captain-kit-sync.

use std::path::Path;

fn same_kit(root: &Path, folder: &Path) {
    for e in std::fs::read_dir(root.join("defaults/captain/kit")).unwrap().flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if !name.ends_with(".js") {
            continue;
        }
        let copy = folder.join("kit").join(&name);
        let got = std::fs::read_to_string(&copy).unwrap_or_else(|_| panic!("{} has no kit/{name}: run scripts/captain-kit-sync", folder.display()));
        assert_eq!(got, std::fs::read_to_string(e.path()).unwrap(), "{} differs from defaults/captain/kit/{name}: run scripts/captain-kit-sync", copy.display());
    }
}

#[test]
fn every_captain_mod_carries_the_same_kit() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let mut seen = 0;
    for e in std::fs::read_dir(root.join("defaults/mods")).unwrap().flatten() {
        if e.file_name().to_string_lossy().starts_with("captain-") {
            same_kit(root, &e.path());
            seen += 1;
        }
    }
    same_kit(root, &root.join("defaults/frame/welcome"));
    assert!(seen >= 2, "expected the captain mods");
}
