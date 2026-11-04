//! A one-paragraph description of a set, written by the `claude` CLI. Optional and best
//! effort: any failure is a warning and no summary.

use std::fs::File;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub const TIMEOUT: Duration = Duration::from_secs(120);

const PROMPT: &str = "\
Write a single succinct paragraph (3-4 sentences) describing the playlist below.
Focus on the overall genres, sound, and mood across the set. Avoid clichés and
goofy phrasing -- write plainly and accurately, as a knowledgeable listener would.
Do not list individual tracks or use bullet points. Output only the paragraph,
with no preamble, heading, or quotation marks.

Tracklist:
";

/// The prompt for these `Artist - Title` lines.
pub fn prompt(track_lines: &[String]) -> String {
    format!("{PROMPT}{}\n", track_lines.join("\n"))
}

/// Runs `claude -p --strict-mcp-config <prompt>` from a throwaway directory, so the nested
/// session picks up no project's CLAUDE.md, hooks or MCP servers. `Err` says why there is
/// no summary.
pub fn generate(claude: &Path, track_lines: &[String]) -> Result<String, String> {
    if track_lines.is_empty() {
        return Err("no identified tracks".into());
    }
    let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
    let out_path = dir.path().join("out");
    let err_path = dir.path().join("err");
    let (out, err) = (
        File::create(&out_path).map_err(|e| e.to_string())?,
        File::create(&err_path).map_err(|e| e.to_string())?,
    );
    let mut child = Command::new(claude)
        .args(["-p", "--strict-mcp-config"])
        .arg(prompt(track_lines))
        .current_dir(dir.path())
        .stdin(Stdio::null())
        .stdout(out)
        .stderr(err)
        .spawn()
        .map_err(|e| format!("could not run {} ({e})", claude.display()))?;
    let deadline = Instant::now() + TIMEOUT;
    let status = loop {
        if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("timed out".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    let read = |p: &Path| std::fs::read_to_string(p).unwrap_or_default();
    if !status.success() {
        return Err(format!("it failed: {}", read(&err_path).trim()));
    }
    let summary = read(&out_path).trim().to_string();
    if summary.is_empty() {
        return Err("it returned nothing".into());
    }
    Ok(summary)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_prompt_is_pythons() {
        let p = prompt(&["A - One".into(), "B - Two".into()]);
        assert!(p.starts_with("Write a single succinct paragraph"));
        assert!(p.ends_with("Tracklist:\nA - One\nB - Two\n"));
    }

    #[cfg(unix)]
    #[test]
    fn runs_the_cli_from_a_temp_dir_and_reports_failures() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let fake = dir.path().join("claude");
        let lines = ["A - One".to_string()];
        let script = |body: &str| {
            std::fs::write(&fake, format!("#!/bin/sh\n{body}\n")).unwrap();
            std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        };
        script(r#"[ "$1 $2" = "-p --strict-mcp-config" ] && echo "  A fine set.  ""#);
        assert_eq!(generate(&fake, &lines), Ok("A fine set.".into()));
        script("echo nope >&2; exit 3");
        assert_eq!(generate(&fake, &lines), Err("it failed: nope".into()));
        script("true");
        assert_eq!(generate(&fake, &lines), Err("it returned nothing".into()));
        assert!(generate(&fake, &[]).is_err());
    }
}
