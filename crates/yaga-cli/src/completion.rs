use std::{
    env,
    ffi::{OsStr, OsString},
    path::{Path, PathBuf},
    process::{Command, ExitCode},
    time::Duration,
};
use sysinfo::{ProcessRefreshKind, ProcessesToUpdate, System};
use yaga_core::{
    completion as profile,
    fs::{absolute_path, display_path},
    git::{
        powershell_executable,
        process::{ProcessError, run_bounded},
    },
    text::safe_text,
};

fn shell_name(name: &str) -> Option<&'static str> {
    let name = Path::new(name.trim())
        .file_stem()?
        .to_str()?
        .trim_start_matches('-')
        .to_ascii_lowercase();
    ["bash", "zsh", "fish", "pwsh", "powershell"]
        .into_iter()
        .find(|s| *s == name)
}
fn detect() -> Result<&'static str, String> {
    const ERROR: &str = "could not detect the current shell; supported shells are bash, zsh, fish, powershell, pwsh";
    if env::var_os("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION").is_some_and(|s| !s.is_empty()) {
        return Err(ERROR.into());
    }
    if let (Ok(mut system), Ok(mut pid)) = (System::new(), sysinfo::get_current_pid()) {
        for _ in 0..16 {
            system.refresh_processes_specifics(
                ProcessesToUpdate::Some(&[pid]),
                false,
                ProcessRefreshKind::nothing(),
            );
            let Some(p) = system.process(pid) else { break };
            if let Some(name) = p.name().to_str().and_then(shell_name) {
                return Ok(name);
            }
            let Some(parent) = p.parent().filter(|id| *id != pid) else {
                break;
            };
            pid = parent;
        }
    }
    env::var("SHELL")
        .ok()
        .as_deref()
        .and_then(shell_name)
        .ok_or_else(|| ERROR.into())
}
fn powershell_profile(shell: &str) -> Result<PathBuf, String> {
    let cwd = env::current_dir().map_err(|_| "could not resolve current directory")?;
    let exe = powershell_executable(&cwd, shell)
        .map_err(|_| format!("shell executable '{shell}' was not found on PATH"))?;
    let mut cmd = Command::new(exe);
    cmd.args(["-NoLogo","-NoProfile","-NonInteractive","-Command","[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); Write-Output $PROFILE.CurrentUserCurrentHost"]);
    let output =
        run_bounded(cmd, None, 16384, 16384, Duration::from_secs(10)).map_err(|e| match e {
            ProcessError::Timeout => format!("timed out querying {shell} profile path"),
            ProcessError::StdoutLimit => {
                format!("{shell} profile path output exceeded 16384 bytes")
            }
            _ => format!("failed to query {shell} profile path"),
        })?;
    if !output.status.success() {
        return Err(format!(
            "could not determine {shell} user profile path (exit {})",
            output.status.code().unwrap_or(-1)
        ));
    }
    let decoded = std::str::from_utf8(&output.stdout)
        .map_err(|_| format!("{shell} profile path is not valid UTF-8"))?;
    let mut lines = decoded.lines().map(str::trim).filter(|s| !s.is_empty());
    let path = lines
        .next()
        .filter(|_| lines.next().is_none())
        .ok_or_else(|| format!("{shell} did not return a single profile path"))?;
    if path.chars().any(|c| u32::from(c) < 32) {
        return Err(format!("{shell} returned an unsafe profile path"));
    }
    Ok(path.into())
}
fn install(shell: &str) -> Result<PathBuf, String> {
    let rendered = profile::script(shell).ok_or("unsupported completion shell")?;
    let home = env::home_dir()
        .and_then(|path| absolute_path(&path).ok())
        .ok_or("could not resolve home directory")?;
    let (path, rc) = match shell {
        "bash" => (
            home.join(".bash_completions/yaga.sh"),
            Some((
                home.join(".bashrc"),
                "source \"$HOME/.bash_completions/yaga.sh\"",
            )),
        ),
        "zsh" => (
            home.join(".zfunc/_yaga"),
            Some((
                home.join(".zshrc"),
                "fpath+=~/.zfunc; autoload -Uz compinit; compinit",
            )),
        ),
        "fish" => (home.join(".config/fish/completions/yaga.fish"), None),
        _ => {
            let path = powershell_profile(shell)?;
            profile::upsert(
                &path,
                rendered,
                if cfg!(windows) { "\r\n" } else { "\n" },
                shell == "powershell",
            )?;
            return Ok(path);
        }
    };
    profile::write(&path, format!("{rendered}\n").as_bytes()).map_err(|_| {
        format!(
            "cannot write completion script {}",
            safe_text(&display_path(&path), 300)
        )
    })?;
    if let Some((rc, body)) = rc {
        profile::upsert(&rc, body, "\n", false)?
    }
    Ok(path)
}
/// Typer handles eager root flags in their original order, before dispatching a subcommand.
pub fn eager_setup(first: Option<&OsStr>, args: impl Iterator<Item = OsString>) -> Option<bool> {
    let show = match first?.to_str()? {
        "--install-completion" => false,
        "--show-completion" => true,
        _ => return None,
    };
    for arg in args {
        let Some(flag) = arg.to_str() else { break };
        if flag == "--" || flag == "-" || !flag.starts_with('-') {
            break;
        }
        if !matches!(
            flag,
            "--show-completion" | "--install-completion" | "--help" | "--version"
        ) {
            // The normal parser owns invalid root option diagnostics.
            return None;
        }
    }
    Some(show)
}

pub fn run(show: bool) -> ExitCode {
    let result = detect().and_then(|s| {
        if show {
            let script = profile::script(s).ok_or("unsupported completion shell")?;
            println!("{script}")
        } else {
            let path = install(s)?;
            println!(
                "{s} completion installed in {}",
                safe_text(&display_path(&path), 500)
            );
            println!("Completion will take effect once you restart the terminal.")
        }
        Ok(())
    });
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("YAGA input error: {}", safe_text(&e, 1000));
            ExitCode::from(2)
        }
    }
}

mod runtime;
pub(crate) use runtime::run as runtime;
