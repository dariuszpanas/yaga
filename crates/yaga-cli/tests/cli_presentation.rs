mod common;

use common::{run, stderr_str, stdout_str, yaga_cmd};

#[test]
fn root_help_and_version_render_typer_rich_layout() {
    let dir = tempfile::tempdir().unwrap();

    let version = run(dir.path(), &["--version"]);
    assert_eq!(version.status.code(), Some(0));
    assert_eq!(
        stdout_str(&version).trim(),
        format!("yaga {}", env!("CARGO_PKG_VERSION"))
    );
    assert!(stderr_str(&version).is_empty());

    let help = run(dir.path(), &["--help"]);
    assert_eq!(help.status.code(), Some(0));
    let text = stdout_str(&help);
    for expected in [
        "Usage:",
        "yaga",
        "Options",
        "Commands",
        "--version",
        "--install-completion",
        "--show-completion",
        "--help",
        "branch",
        "change",
        "commit",
        "config",
        "gate",
        "github",
        "mode",
        "path",
        "policy",
        "pull-request",
        "repo",
        "self",
        "size",
        "test",
        "tree",
        "workflow",
    ] {
        assert!(text.contains(expected), "missing {expected:?} in:\n{text}");
    }
    // Box borders from fast-rich panel rendering (rounded on VT/POSIX, square on Windows pipe).
    assert!(
        text.contains('╭') || text.contains('┌'),
        "expected framed panels:\n{text}"
    );
}

#[test]
fn all_command_groups_render_help_cleanly() {
    let dir = tempfile::tempdir().unwrap();
    for group in [
        "branch",
        "change",
        "commit",
        "config",
        "gate",
        "github",
        "mode",
        "path",
        "policy",
        "pull-request",
        "repo",
        "self",
        "size",
        "test",
        "tree",
        "workflow",
    ] {
        let out = run(dir.path(), &[group, "--help"]);
        assert_eq!(
            out.status.code(),
            Some(0),
            "{group} --help failed:\n{}",
            stderr_str(&out)
        );
        let text = stdout_str(&out);
        assert!(text.contains("Usage:"), "{group} help missing Usage");
        assert!(text.contains("Commands"), "{group} help missing Commands");
    }
}

fn has_ansi_foreground_color(text: &str) -> bool {
    let mut rest = text;
    while let Some(idx) = rest.find("\x1b[") {
        rest = &rest[idx + 2..];
        let Some(end) = rest.find('m') else {
            break;
        };
        if rest[..end]
            .split(';')
            .any(|code| matches!(code, "30" | "31" | "32" | "33" | "34" | "35" | "36" | "37"))
        {
            return true;
        }
        rest = &rest[end + 1..];
    }
    false
}

#[test]
fn force_color_emits_ansi_and_no_color_suppresses_it() {
    let dir = tempfile::tempdir().unwrap();

    let colored = yaga_cmd(dir.path())
        .env_remove("NO_COLOR")
        .env("FORCE_COLOR", "1")
        .arg("--help")
        .output()
        .unwrap();
    assert_eq!(colored.status.code(), Some(0));
    #[cfg(not(windows))]
    assert!(has_ansi_foreground_color(&stdout_str(&colored)));

    // Like Rich, NO_COLOR=1 suppresses foreground ANSI color codes even when
    // FORCE_COLOR=1 keeps terminal styling active.
    let no_color = yaga_cmd(dir.path())
        .env("NO_COLOR", "1")
        .env("FORCE_COLOR", "1")
        .arg("--help")
        .output()
        .unwrap();
    assert_eq!(no_color.status.code(), Some(0));
    assert!(!has_ansi_foreground_color(&stdout_str(&no_color)));

    // On a non-TTY pipe without forced styling, no ANSI escapes are emitted.
    let plain = yaga_cmd(dir.path())
        .env("NO_COLOR", "1")
        .arg("--help")
        .output()
        .unwrap();
    assert_eq!(plain.status.code(), Some(0));
    assert!(!stdout_str(&plain).contains("\x1b["));
}

#[test]
fn usage_errors_render_framed_diagnostics_with_exit_2() {
    let dir = tempfile::tempdir().unwrap();

    let unknown = run(dir.path(), &["--does-not-exist"]);
    assert_eq!(unknown.status.code(), Some(2));
    assert!(stdout_str(&unknown).is_empty());
    let err = stderr_str(&unknown);
    assert!(err.contains("Error"), "expected Error panel:\n{err}");
    assert!(err.contains("--does-not-exist"), "{err}");

    let missing = run(dir.path(), &["branch", "check"]);
    assert_eq!(missing.status.code(), Some(2));
    let err = stderr_str(&missing);
    assert!(err.contains("Missing option"), "{err}");
    assert!(err.contains("--policy"), "{err}");

    let bad_format = run(
        dir.path(),
        &[
            "commit",
            "check",
            "--message",
            "feat: ok",
            "--format",
            "xml",
        ],
    );
    assert_eq!(bad_format.status.code(), Some(2));
    assert!(stderr_str(&bad_format).contains("Invalid value"));
}

#[test]
fn shell_completion_show_and_runtime_queries_work() {
    let dir = tempfile::tempdir().unwrap();

    // Deterministic script rendering for every supported shell via the runtime
    // source protocol (independent of the test runner's parent process tree).
    for (shell, marker) in [
        ("bash", "_yaga_completion"),
        ("zsh", "#compdef yaga"),
        ("fish", "complete --command yaga"),
        ("pwsh", "Register-ArgumentCompleter"),
    ] {
        let out = yaga_cmd(dir.path())
            .env("_YAGA_COMPLETE", format!("source_{shell}"))
            .output()
            .unwrap();
        assert_eq!(
            out.status.code(),
            Some(0),
            "source_{shell} failed:\n{}",
            stderr_str(&out)
        );
        let text = stdout_str(&out);
        assert!(
            text.contains(marker),
            "expected {marker} for {shell}:\n{text}"
        );
    }

    // Disabling shell detection causes --show-completion to fail closed (exit 2).
    let undetected = yaga_cmd(dir.path())
        .env("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION", "1")
        .arg("--show-completion")
        .output()
        .unwrap();
    assert_eq!(undetected.status.code(), Some(2));
    assert!(stderr_str(&undetected).contains("could not detect the current shell"));

    // Dynamic completion protocol query for root subcommands.
    let complete = yaga_cmd(dir.path())
        .env("_YAGA_COMPLETE", "complete_bash")
        .env("COMP_WORDS", "yaga com")
        .env("COMP_CWORD", "1")
        .output()
        .unwrap();
    assert_eq!(complete.status.code(), Some(0));
    let out = stdout_str(&complete);
    assert!(out.contains("commit"), "expected commit completion:\n{out}");
}

#[test]
fn bare_invocation_without_args_prints_help() {
    let dir = tempfile::tempdir().unwrap();
    let out = run(dir.path(), &[]);
    assert!(matches!(out.status.code(), Some(0) | Some(2)));
    let combined = format!("{}{}", stdout_str(&out), stderr_str(&out));
    assert!(combined.contains("Usage:"), "{combined}");
}
