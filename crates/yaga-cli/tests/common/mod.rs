#![allow(dead_code)]

use serde_json::Value;
use std::{
    fs,
    io::Write,
    path::Path,
    process::{Command, Output, Stdio},
};

pub fn yaga_cmd(cwd: &Path) -> Command {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_yaga"));
    cmd.current_dir(cwd)
        .env("NO_COLOR", "1")
        .env("COLUMNS", "100")
        .env_remove("FORCE_COLOR")
        .env_remove("CLICOLOR_FORCE")
        .env_remove("PY_COLORS")
        .env_remove("GITHUB_ACTIONS")
        .env_remove("_TYPER_FORCE_DISABLE_TERMINAL")
        .env_remove("YAGA_GLOBAL_CONFIG");
    cmd
}

pub fn run(cwd: &Path, args: &[&str]) -> Output {
    yaga_cmd(cwd).args(args).output().expect("run yaga")
}

pub fn run_with_stdin(cwd: &Path, args: &[&str], stdin_bytes: &[u8]) -> Output {
    let mut child = yaga_cmd(cwd)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn yaga");
    child
        .stdin
        .take()
        .expect("stdin handle")
        .write_all(stdin_bytes)
        .expect("write stdin");
    child.wait_with_output().expect("wait yaga")
}

pub fn stdout_str(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("UTF-8 stdout")
}

pub fn stderr_str(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).expect("UTF-8 stderr")
}

pub fn stdout_json(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|e| {
        panic!(
            "expected JSON stdout, got error {e}\nstdout:\n{}\nstderr:\n{}",
            stdout_str(output),
            stderr_str(output)
        )
    })
}

pub fn stderr_json(output: &Output) -> Value {
    serde_json::from_slice(&output.stderr).unwrap_or_else(|e| {
        panic!(
            "expected JSON stderr, got error {e}\nstdout:\n{}\nstderr:\n{}",
            stdout_str(output),
            stderr_str(output)
        )
    })
}

pub fn git(repo: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(repo)
        .args(args)
        .output()
        .expect("run git");
    assert!(
        output.status.success(),
        "git {:?} failed: {}",
        args,
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)
        .expect("UTF-8 git stdout")
        .trim()
        .to_owned()
}

pub fn init_git_repo(repo: &Path) {
    git(repo, &["init", "--initial-branch=main"]);
    git(repo, &["config", "user.name", "YAGA Test"]);
    git(repo, &["config", "user.email", "test@example.invalid"]);
    git(repo, &["config", "commit.gpgsign", "false"]);
}

pub fn write_file(root: &Path, rel: &str, content: &[u8]) {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create parent dirs");
    }
    fs::write(&path, content).expect("write fixture file");
}

pub fn commit_all(repo: &Path, message: &str) -> String {
    git(repo, &["add", "-A"]);
    git(repo, &["commit", "--allow-empty", "-m", message]);
    git(repo, &["rev-parse", "HEAD"])
}
