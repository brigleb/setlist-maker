//! Finding the `claude` CLI from an app Finder launched, whose PATH is only
//! `/usr/bin:/bin:/usr/sbin:/sbin`: ask the user's login shell, then try the usual places.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// How long the login shell may take to answer (a slow profile shouldn't hang the lookup).
const SHELL_TIMEOUT: Duration = Duration::from_secs(5);

fn executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    p.metadata()
        .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

/// `$SHELL -lc 'command -v claude'`, else `~/.local/bin`, `/opt/homebrew/bin`,
/// `/usr/local/bin`, `~/.claude/local`.
pub fn find_claude() -> Option<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    from_login_shell().or_else(|| {
        let mut dirs: Vec<PathBuf> = Vec::new();
        if let Some(h) = &home {
            dirs.push(h.join(".local/bin"));
        }
        dirs.push("/opt/homebrew/bin".into());
        dirs.push("/usr/local/bin".into());
        if let Some(h) = &home {
            dirs.push(h.join(".claude/local"));
        }
        dirs.into_iter()
            .map(|d| d.join("claude"))
            .find(|p| executable(p))
    })
}

fn from_login_shell() -> Option<PathBuf> {
    let shell = std::env::var_os("SHELL").unwrap_or_else(|| "/bin/zsh".into());
    let mut child = Command::new(shell)
        .args(["-lc", "command -v claude"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if started.elapsed() < SHELL_TIMEOUT => {
                std::thread::sleep(Duration::from_millis(20));
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
    let mut out = String::new();
    use std::io::Read;
    child.stdout.take()?.read_to_string(&mut out).ok()?;
    // A profile may print other lines first; `command -v` prints the path last.
    let path = PathBuf::from(out.lines().last()?.trim());
    (path.is_absolute() && executable(&path)).then_some(path)
}
