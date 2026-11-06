//! The DMG has no installer to put the app's CLI on PATH. CAPTAIN: the link is ~/.local/bin/captain-app to Captain.app, so Herdr's herdr-app link is never touched.

use std::io;
use std::path::Path;

fn herdr_bundle(exe: &Path) -> bool {
    exe.is_absolute() && exe.ends_with(Path::new(crate::brand::BUNDLE).join("Contents/MacOS").join(crate::brand::BUNDLE_EXE))
}

fn installed_bundle(exe: &Path) -> bool {
    herdr_bundle(exe)
        && !exe.starts_with("/Volumes")
        && !exe.components().any(|c| c.as_os_str() == "AppTranslocation" || c.as_os_str() == "target")
}

pub(super) fn ensure(home: &Path, exe: &Path) -> io::Result<()> {
    if !installed_bundle(exe) {
        return Ok(());
    }
    let link = home.join(".local/bin").join(crate::brand::BIN);
    match std::fs::symlink_metadata(&link) {
        Ok(meta) if meta.file_type().is_symlink() => {
            let target = std::fs::read_link(&link)?;
            if target == exe {
                return Ok(());
            }
            let resolved = if target.is_absolute() { target } else { link.parent().unwrap().join(target) };
            if !herdr_bundle(&resolved) {
                return Ok(());
            }
            std::fs::remove_file(&link)?;
        }
        Ok(_) => return Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    std::fs::create_dir_all(link.parent().unwrap())?;
    std::os::unix::fs::symlink(exe, link)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn repairs_only_our_cli_link() {
        let home = std::env::temp_dir().join(format!("captain-cli-link-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&home);
        std::fs::create_dir_all(&home).unwrap();
        let exe = home.join("Applications/Captain.app/Contents/MacOS/Captain");
        let link = home.join(".local/bin/captain-app");
        ensure(&home, &exe).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), exe);
        // Running it again leaves the same link alone.
        ensure(&home, &exe).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), exe);

        for old in [
            home.join("old/Captain.app/Contents/MacOS/Captain"),
            "../../Applications/Captain.app/Contents/MacOS/Captain".into(),
            "/Volumes/Captain/Captain.app/Contents/MacOS/Captain".into(),
            "/private/var/folders/AppTranslocation/old/d/Captain.app/Contents/MacOS/Captain".into(),
        ] {
            std::fs::remove_file(&link).unwrap();
            symlink(old, &link).unwrap();
            ensure(&home, &exe).unwrap();
            assert_eq!(std::fs::read_link(&link).unwrap(), exe);
        }
        let old = home.join("previous/Captain.app/Contents/MacOS/Captain");
        std::fs::create_dir_all(old.parent().unwrap()).unwrap();
        std::fs::write(&old, "previous executable").unwrap();
        std::fs::remove_file(&link).unwrap();
        symlink(&old, &link).unwrap();
        ensure(&home, &exe).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), exe);
        assert_eq!(std::fs::read_to_string(&old).unwrap(), "previous executable");

        std::fs::remove_file(&link).unwrap();
        std::fs::write(&link, "personal CLI").unwrap();
        ensure(&home, &exe).unwrap();
        assert_eq!(std::fs::read_to_string(&link).unwrap(), "personal CLI");
        std::fs::remove_file(&link).unwrap();
        let foreign = home.join("other-app");
        symlink(&foreign, &link).unwrap();
        ensure(&home, &exe).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), foreign);
        std::fs::remove_file(&link).unwrap();

        for skipped in [
            "/Volumes/Captain/Captain.app/Contents/MacOS/Captain",
            "/private/var/folders/AppTranslocation/random/d/Captain.app/Contents/MacOS/Captain",
            "/work/target/debug/Captain.app/Contents/MacOS/Captain",
            "/work/target/debug/herdr-app",
            "/Applications/Other.app/Contents/MacOS/Herdr",
        ] {
            ensure(&home, Path::new(skipped)).unwrap();
            assert!(link.symlink_metadata().is_err(), "{skipped}");
        }
        std::fs::remove_dir_all(home).unwrap();
    }
}
