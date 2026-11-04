//! Atomic writes and the config folder: the two things every saved file goes through.

use std::ffi::OsStr;
use std::fs;
use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// The folder name under `$XDG_CONFIG_HOME` (and `$XDG_CACHE_HOME`).
pub const APP_DIR: &str = "hindsight";
/// What the Python version called its folder, adopted on first launch.
pub const PYTHON_APP_DIR: &str = "setlist-maker";

/// `$XDG_CONFIG_HOME/hindsight`, else `~/.config/hindsight`, on macOS too (ADR 0003).
pub fn config_dir() -> PathBuf {
    config_base(std::env::var_os("XDG_CONFIG_HOME").as_deref(), &home()).join(APP_DIR)
}

/// The XDG rule without the process environment, so tests needn't set variables that every
/// test thread shares. An empty variable counts as unset, as in Python's `or`.
pub fn config_base(xdg_config_home: Option<&OsStr>, home: &Path) -> PathBuf {
    match xdg_config_home {
        Some(v) if !v.is_empty() => PathBuf::from(v),
        _ => home.join(".config"),
    }
}

fn home() -> PathBuf {
    std::env::home_dir().unwrap_or_default()
}

/// Copies Python's `setlist-maker` config folder to `hindsight` the first time, so the
/// corrections it learned come across. A copy, never a move: Python keeps working until the
/// cutover. Does nothing once `hindsight` exists. The copy is made beside it and renamed into
/// place, so a crash halfway leaves no half-filled folder that would stop the next try.
///
/// Returns whether it copied anything.
pub fn adopt_python_config(base: &Path) -> io::Result<bool> {
    let ours = base.join(APP_DIR);
    let theirs = base.join(PYTHON_APP_DIR);
    if ours.exists() || !theirs.is_dir() {
        return Ok(false);
    }
    let staging = tempfile::Builder::new()
        .prefix(".hindsight.")
        .tempdir_in(base)?;
    for entry in fs::read_dir(&theirs)? {
        let entry = entry?;
        // Following symlinks: a linked corrections.json is still the user's corrections.
        if fs::metadata(entry.path()).is_ok_and(|m| m.is_file()) {
            fs::copy(entry.path(), staging.path().join(entry.file_name()))?;
        }
    }
    fs::rename(staging.keep(), &ours)?;
    Ok(true)
}

/// Replaces `path` with `data` so a reader never sees a partial file.
///
/// Written to a temp file in the same folder, synced, then renamed over the target: a crash,
/// a full disk or a kill midway leaves the previous file, and the temp file is removed on
/// failure. The replaced file keeps its permissions, and a symlink keeps pointing where it
/// did (the file it points at is what's replaced). A new file gets `0o666` less the umask,
/// which the kernel applies when the temp file is created.
pub fn atomic_write(path: &Path, data: &[u8]) -> io::Result<()> {
    let path = resolve_links(path)?;
    let dir = match path.parent() {
        Some(d) if !d.as_os_str().is_empty() => d.to_path_buf(),
        _ => PathBuf::from("."),
    };
    fs::create_dir_all(&dir)?;
    let mode = match fs::metadata(&path) {
        Ok(m) => Some(m.permissions().mode() & 0o777),
        Err(e) if e.kind() == io::ErrorKind::NotFound => None,
        Err(e) => return Err(e),
    };
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let mut tmp = tempfile::Builder::new()
        .prefix(&format!(".{name}."))
        .suffix(".tmp")
        .permissions(fs::Permissions::from_mode(0o666))
        .tempfile_in(&dir)?;
    tmp.write_all(data)?;
    tmp.as_file().sync_all()?;
    if let Some(mode) = mode {
        tmp.as_file()
            .set_permissions(fs::Permissions::from_mode(mode))?;
    }
    tmp.persist(&path).map_err(|e| e.error)?;
    Ok(())
}

/// Follows a chain of symlinks to the file it ends at, which need not exist yet (Python's
/// `Path.resolve()` on a dangling link). A link loop is an error rather than a hang.
fn resolve_links(path: &Path) -> io::Result<PathBuf> {
    let mut path = path.to_path_buf();
    for _ in 0..40 {
        match fs::symlink_metadata(&path) {
            Ok(m) if m.file_type().is_symlink() => {
                let target = fs::read_link(&path)?;
                path = match path.parent() {
                    Some(parent) => parent.join(target),
                    None => target,
                };
            }
            _ => return Ok(path),
        }
    }
    Err(io::Error::other(format!(
        "too many symlinks at {}",
        path.display()
    )))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn umask() -> u32 {
        // Read the way a new file would see it, without touching the process umask.
        let dir = tempfile::tempdir().unwrap();
        let probe = dir.path().join("probe");
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&probe)
            .unwrap();
        0o666 & !(fs::metadata(&probe).unwrap().permissions().mode() & 0o777)
    }

    fn mode(path: &Path) -> u32 {
        fs::metadata(path).unwrap().permissions().mode() & 0o777
    }

    fn names(dir: &Path) -> Vec<String> {
        let mut v: Vec<String> = fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        v.sort();
        v
    }

    #[test]
    fn a_rewritten_file_keeps_its_permissions() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("notes.md");
        fs::write(&path, "old").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
        atomic_write(&path, b"new").unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "new");
        assert_eq!(mode(&path), 0o640);
    }

    #[test]
    fn a_new_file_gets_ordinary_permissions_not_a_temp_files() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("fresh.md");
        atomic_write(&path, b"x").unwrap();
        assert_eq!(mode(&path), 0o666 & !umask());
    }

    #[test]
    fn writing_through_a_symlink_keeps_the_link() {
        let dir = tempfile::tempdir().unwrap();
        let real = dir.path().join("real.md");
        fs::write(&real, "old").unwrap();
        let link = dir.path().join("link.md");
        symlink(&real, &link).unwrap();
        atomic_write(&link, b"new").unwrap();
        assert!(
            fs::symlink_metadata(&link)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(fs::read_to_string(&real).unwrap(), "new");
    }

    #[test]
    fn a_dangling_symlink_creates_its_target() {
        let dir = tempfile::tempdir().unwrap();
        let link = dir.path().join("link.md");
        symlink("real.md", &link).unwrap();
        atomic_write(&link, b"new").unwrap();
        assert_eq!(
            fs::read_to_string(dir.path().join("real.md")).unwrap(),
            "new"
        );
    }

    #[test]
    fn missing_folders_are_created() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a/b/c.json");
        atomic_write(&path, b"{}").unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "{}");
    }

    #[test]
    fn a_failed_rename_leaves_the_folder_as_it_was() {
        // A non-empty folder where the file should go: the temp file is written, the rename
        // fails, and the temp file must not be left behind.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("set_tracklist.md");
        fs::create_dir(&path).unwrap();
        fs::write(path.join("keep"), "x").unwrap();
        assert!(atomic_write(&path, b"new").is_err());
        assert_eq!(names(dir.path()), ["set_tracklist.md"]);
        assert_eq!(names(&path), ["keep"]);
    }

    #[test]
    fn a_folder_it_cant_write_to_leaves_the_previous_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("set_tracklist.md");
        fs::write(&path, "before").unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o555)).unwrap();
        let result = atomic_write(&path, b"after");
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(result.is_err());
        assert_eq!(fs::read_to_string(&path).unwrap(), "before");
        assert_eq!(names(dir.path()), ["set_tracklist.md"]);
    }

    #[test]
    fn config_dir_follows_xdg_config_home_else_dot_config() {
        let home = Path::new("/Users/someone");
        assert_eq!(
            config_base(Some(OsStr::new("/x/conf")), home),
            Path::new("/x/conf")
        );
        assert_eq!(config_base(None, home), home.join(".config"));
        assert_eq!(
            config_base(Some(OsStr::new("")), home),
            home.join(".config")
        );
    }

    #[test]
    fn pythons_config_is_copied_once_and_left_in_place() {
        let base = tempfile::tempdir().unwrap();
        let theirs = base.path().join(PYTHON_APP_DIR);
        fs::create_dir(&theirs).unwrap();
        fs::write(theirs.join("corrections.json"), r#"{"corrections": {}}"#).unwrap();
        fs::write(theirs.join("config.json"), r#"{"episode_artist": "DJ"}"#).unwrap();

        assert!(adopt_python_config(base.path()).unwrap());
        let ours = base.path().join(APP_DIR);
        assert_eq!(names(&ours), ["config.json", "corrections.json"]);
        assert_eq!(names(&theirs), ["config.json", "corrections.json"]);
        assert_eq!(names(base.path()), [APP_DIR, PYTHON_APP_DIR]);

        // Later launches leave ours alone, even when Python has learned more since.
        fs::write(theirs.join("corrections.json"), "newer").unwrap();
        assert!(!adopt_python_config(base.path()).unwrap());
        assert_eq!(
            fs::read_to_string(ours.join("corrections.json")).unwrap(),
            r#"{"corrections": {}}"#
        );
    }

    #[test]
    fn a_symlinked_file_is_copied_as_its_contents() {
        let base = tempfile::tempdir().unwrap();
        let theirs = base.path().join(PYTHON_APP_DIR);
        fs::create_dir(&theirs).unwrap();
        let real = base.path().join("elsewhere.json");
        fs::write(&real, "{}").unwrap();
        symlink(&real, theirs.join("corrections.json")).unwrap();
        assert!(adopt_python_config(base.path()).unwrap());
        let copied = base.path().join(APP_DIR).join("corrections.json");
        assert!(
            !fs::symlink_metadata(&copied)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(fs::read_to_string(copied).unwrap(), "{}");
    }

    #[test]
    fn nothing_to_adopt_creates_nothing() {
        let base = tempfile::tempdir().unwrap();
        assert!(!adopt_python_config(base.path()).unwrap());
        assert!(names(base.path()).is_empty());
    }
}
