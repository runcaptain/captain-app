//! A log file that starts a new one when it is full: app.log, then app.log.1 (the one before) up to app.log.5.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

pub struct RotatingFile {
    path: PathBuf,
    max: u64,
    keep: usize,
    file: Option<File>,
    size: u64,
}

/// `app.log` and number 2 give `app.log.2`.
pub fn numbered(path: &Path, n: usize) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(format!(".{n}"));
    PathBuf::from(name)
}

impl RotatingFile {
    /// Nothing is opened until the first line is written.
    pub fn new(path: PathBuf, max: u64, keep: usize) -> Self {
        Self { path, max, keep, file: None, size: 0 }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn open(&mut self) -> std::io::Result<&mut File> {
        if self.file.is_none() {
            if let Some(dir) = self.path.parent() {
                std::fs::create_dir_all(dir)?;
            }
            let file = OpenOptions::new().create(true).append(true).open(&self.path)?;
            self.size = file.metadata().map(|m| m.len()).unwrap_or(0);
            self.file = Some(file);
        }
        Ok(self.file.as_mut().expect("just opened"))
    }

    fn rotate(&mut self) {
        self.file = None;
        let _ = std::fs::remove_file(numbered(&self.path, self.keep));
        for n in (1..self.keep).rev() {
            let _ = std::fs::rename(numbered(&self.path, n), numbered(&self.path, n + 1));
        }
        if self.keep == 0 {
            let _ = std::fs::remove_file(&self.path);
        } else {
            let _ = std::fs::rename(&self.path, numbered(&self.path, 1));
        }
        self.size = 0;
    }

    /// Append `text` (whole lines, each ending in a newline) in one write, so a crash never leaves half of it.
    pub fn write(&mut self, text: &str) -> std::io::Result<()> {
        self.open()?;
        if self.size > 0 && self.size + text.len() as u64 > self.max {
            self.rotate();
        }
        let file = self.open()?;
        file.write_all(text.as_bytes())?;
        self.size += text.len() as u64;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_full_log_starts_a_new_file_and_keeps_only_so_many() {
        let dir = std::env::temp_dir().join(format!("captain-logs-rotate-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("app.log");
        let mut log = RotatingFile::new(path.clone(), 100, 2);
        let line = |n: usize| format!("{n:02} {}\n", "x".repeat(56));
        // 60 bytes a line, 100 allowed: every line after the first starts a new file.
        for n in 0..5 {
            log.write(&line(n)).unwrap();
        }
        assert_eq!(std::fs::read_to_string(&path).unwrap(), line(4));
        assert_eq!(std::fs::read_to_string(numbered(&path, 1)).unwrap(), line(3));
        assert_eq!(std::fs::read_to_string(numbered(&path, 2)).unwrap(), line(2));
        assert!(!numbered(&path, 3).exists(), "only two old files are kept");
        // A later start appends to the same file and counts what is already in it.
        let mut again = RotatingFile::new(path.clone(), 100, 2);
        again.write("short\n").unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), format!("{}short\n", line(4)));
        again.write(&line(9)).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), line(9));
        // A line longer than the limit is still written whole, to an empty file.
        let long = format!("{}\n", "y".repeat(300));
        again.write(&long).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), long);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
