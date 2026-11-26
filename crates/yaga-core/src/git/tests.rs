use super::process::{GroupKill, ProcessError, run_bounded, terminate_macos_group};
use super::*;
use std::cell::Cell;
use std::io::{self, Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

fn git(directory: &Path, args: &[&str]) -> Output {
    let result = Command::new("git")
        .arg("-C")
        .arg(directory)
        .args(args)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env(
            "GIT_CONFIG_GLOBAL",
            if cfg!(windows) { "nul" } else { "/dev/null" },
        )
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&result.stderr)
    );
    result
}

#[test]
fn exact_commit_tree_and_changed_paths_with_overlay_checks() {
    let temp = tempfile::tempdir().unwrap();
    let repo = temp.path();
    git(repo, &["init", "-q"]);
    git(repo, &["config", "user.name", "Test"]);
    git(repo, &["config", "user.email", "test@example.invalid"]);
    std::fs::write(repo.join("first"), "one").unwrap();
    git(repo, &["add", "first"]);
    git(repo, &["commit", "-qm", "initial"]);
    std::fs::write(repo.join("second"), "two").unwrap();
    git(repo, &["add", "second"]);
    git(repo, &["commit", "-qm", "next"]);
    let sub = repo.join("subdir");
    std::fs::create_dir(&sub).unwrap();
    if !Command::new("git")
        .args(["--no-lazy-fetch", "version"])
        .output()
        .unwrap()
        .status
        .success()
    {
        assert!(
            resolve_committed_tree(&sub, "HEAD", GitTreeKind::Tree).is_err(),
            "older Git must fail closed"
        );
        return;
    }
    let selected = resolve_committed_tree(&sub, "HEAD", GitTreeKind::Tree).unwrap();
    assert_eq!(
        selected.commit_sha(),
        parse_object_id(&git(repo, &["rev-parse", "HEAD"]).stdout, "commit").unwrap()
    );
    assert_eq!(
        selected.enumerate(GitTreeKind::Tree).unwrap(),
        b"first\0second\0"
    );
    assert_eq!(
        read_changed_paths(&sub, "HEAD^...HEAD", true)
            .unwrap()
            .paths,
        ["second"]
    );
    let sizes = selected.enumerate(GitTreeKind::Size).unwrap();
    crate::paths::records::SizeSelection::parse(&sizes, selected.tree_sha().len()).unwrap();
    let repo_handle = Repository::open(&sub).unwrap();
    assert_eq!(
        repo_handle
            .strip_editor_comments(b"# comments\nsubject\n".to_vec(), 4096)
            .unwrap(),
        b"subject\n"
    );
    std::fs::write(repo.join(".git/info/grafts"), "legacy\n").unwrap();
    assert!(selected.enumerate(GitTreeKind::Tree).is_err());
    assert!(read_changed_paths(repo, "HEAD^..HEAD", true).is_err());
}

#[test]
fn revisions_and_ids_fail_before_unsafe_selection() {
    for name in [
        "-n",
        "^abc",
        "HEAD^!",
        "abc..def",
        "a:b",
        "bad\u{200b}",
        "a\nb",
        "HEAD^@",
        "HEAD^-2",
    ] {
        assert!(matches!(
            resolve_committed_tree(Path::new("missing"), name, GitTreeKind::Path),
            Err(GitError::Input(_))
        ));
    }
    for bytes in [
        vec![],
        vec![b'a'; 39],
        vec![b'F'; 40],
        vec![b'g'; 64],
        [vec![b'2'; 40], b"\n\n".to_vec()].concat(),
    ] {
        assert!(parse_object_id(&bytes, "commit").is_err());
    }
}

fn fixture(kind: &str) -> Command {
    let mut c = Command::new(std::env::current_exe().unwrap());
    c.args([
        "--ignored",
        "--exact",
        "git::tests::pipe_fixture",
        "--nocapture",
    ])
    .env("YAGA_NATIVE_PIPE_FIXTURE", kind);
    c
}

#[test]
#[ignore = "launched as a controlled child process by bounded_process_limits"]
fn pipe_fixture() {
    match std::env::var("YAGA_NATIVE_PIPE_FIXTURE").as_deref() {
        Ok("stdout") => {
            if let Err(error) = std::io::stdout().write_all(&vec![b'x'; 131072])
                && error.kind() != io::ErrorKind::BrokenPipe
            {
                panic!("unexpected stdout fixture write error: {error}");
            }
        }
        Ok("stderr") => {
            if let Err(error) = std::io::stderr().write_all(&vec![b'e'; 131072])
                && error.kind() != io::ErrorKind::BrokenPipe
            {
                panic!("unexpected stderr fixture write error: {error}");
            }
        }
        Ok("echo") => {
            let mut data = Vec::new();
            std::io::stdin().read_to_end(&mut data).unwrap();
            std::io::stdout().write_all(&data).unwrap();
        }
        Ok("sleep") => std::thread::sleep(Duration::from_secs(60)),
        Ok("descendant") => {
            // A grandchild retaining the pipe keeps readers alive after the leader exits.
            #[allow(
                clippy::zombie_processes,
                reason = "this fixture tests cleanup after the pipe-owning parent exits"
            )]
            let _child = fixture("sleep").stdin(Stdio::null()).spawn().unwrap();
        }
        _ => {}
    }
}

#[test]
fn bounded_process_limits() {
    for kind in ["stdout", "stderr"] {
        let error =
            run_bounded(fixture(kind), None, 1024, 1024, Duration::from_secs(5)).unwrap_err();
        assert!(
            matches!(
                (kind, &error),
                ("stdout", ProcessError::StdoutLimit) | ("stderr", ProcessError::StderrLimit)
            ),
            "{kind}: {error:?}"
        );
    }
    let out = run_bounded(
        fixture("echo"),
        Some(vec![b'z'; 131072]),
        150000,
        1024,
        Duration::from_secs(5),
    )
    .unwrap();
    assert!(out.status.success());
    assert!(out.stdout.windows(32768).any(|w| w == [b'z'; 32768]));
    let start = Instant::now();
    let result = run_bounded(
        fixture("descendant"),
        None,
        8192,
        1024,
        Duration::from_millis(450),
    );
    assert!(matches!(result, Err(ProcessError::Timeout)), "{result:?}");
    assert!(start.elapsed() < Duration::from_secs(4));
}

#[test]
fn repeated_early_exit_pipe_limit_terminations() {
    for iteration in 0..5 {
        for kind in ["stdout", "stderr"] {
            let error =
                run_bounded(fixture(kind), None, 1024, 1024, Duration::from_secs(5)).unwrap_err();
            assert!(
                matches!(
                    (kind, &error),
                    ("stdout", ProcessError::StdoutLimit) | ("stderr", ProcessError::StderrLimit)
                ),
                "iteration {iteration} ({kind}): {error:?}"
            );
        }
    }
}

#[test]
fn macos_group_termination_retries_transient_eperm_and_fails_closed() {
    let tick = Duration::from_millis(2);

    // Immediate Done never polls exited() or sleeps.
    let base = Instant::now();
    let mut exited_calls = 0;
    let mut sleeps = Vec::new();
    terminate_macos_group(
        base + Duration::from_millis(10),
        tick,
        || base,
        || Ok(GroupKill::Done),
        || {
            exited_calls += 1;
            Ok(false)
        },
        |duration| sleeps.push(duration),
    )
    .unwrap();
    assert_eq!(exited_calls, 0);
    assert!(sleeps.is_empty());

    // Immediate EPERM when leader is already a zombie (SZOMB) completes without sleeping.
    let mut exited_calls = 0;
    let mut sleeps = Vec::new();
    terminate_macos_group(
        base + Duration::from_millis(10),
        tick,
        || base,
        || Ok(GroupKill::PermissionDenied(io::Error::from_raw_os_error(1))),
        || {
            exited_calls += 1;
            Ok(true)
        },
        |duration| sleeps.push(duration),
    )
    .unwrap();
    assert_eq!(exited_calls, 1);
    assert!(sleeps.is_empty());

    // Transient EPERM while leader is in P_LEXIT retries until exited() reports true.
    let clock = Cell::new(base);
    let mut kill_calls = 0;
    let mut exited_calls = 0;
    let mut sleeps = Vec::new();
    terminate_macos_group(
        base + Duration::from_millis(10),
        tick,
        || clock.get(),
        || {
            kill_calls += 1;
            Ok(GroupKill::PermissionDenied(io::Error::from_raw_os_error(1)))
        },
        || {
            exited_calls += 1;
            Ok(exited_calls >= 3)
        },
        |duration| {
            sleeps.push(duration);
            clock.set(clock.get() + duration);
        },
    )
    .unwrap();
    assert_eq!(kill_calls, 3);
    assert_eq!(exited_calls, 3);
    assert_eq!(sleeps, [tick, tick]);

    // Transient EPERM resolves when a subsequent killpg returns Ok(()) or ESRCH (Done).
    let clock = Cell::new(base);
    let mut kill_calls = 0;
    let mut exited_calls = 0;
    let mut sleeps = Vec::new();
    terminate_macos_group(
        base + Duration::from_millis(10),
        tick,
        || clock.get(),
        || {
            kill_calls += 1;
            if kill_calls == 1 {
                Ok(GroupKill::PermissionDenied(io::Error::from_raw_os_error(1)))
            } else {
                Ok(GroupKill::Done)
            }
        },
        || {
            exited_calls += 1;
            Ok(false)
        },
        |duration| {
            sleeps.push(duration);
            clock.set(clock.get() + duration);
        },
    )
    .unwrap();
    assert_eq!(kill_calls, 2);
    assert_eq!(exited_calls, 1);
    assert_eq!(sleeps, [tick]);

    // Permanent EPERM stays fail-closed after the bounded deadline, clamping the final tick
    // and preserving the raw OS error.
    let clock = Cell::new(base);
    let mut kill_calls = 0;
    let mut exited_calls = 0;
    let mut sleeps = Vec::new();
    let error = terminate_macos_group(
        base + Duration::from_millis(5),
        tick,
        || clock.get(),
        || {
            kill_calls += 1;
            Ok(GroupKill::PermissionDenied(io::Error::from_raw_os_error(1)))
        },
        || {
            exited_calls += 1;
            Ok(false)
        },
        |duration| {
            sleeps.push(duration);
            clock.set(clock.get() + duration);
        },
    )
    .unwrap_err();
    assert_eq!(error.raw_os_error(), Some(1));
    assert_eq!(sleeps, [tick, tick, Duration::from_millis(1)]);
    assert_eq!(kill_calls, 4);
    assert_eq!(exited_calls, 4);

    // Zero-budget deadline (used by Drop) performs a single check without sleeping.
    let mut kill_calls = 0;
    let mut exited_calls = 0;
    let mut sleeps = Vec::new();
    let error = terminate_macos_group(
        base,
        tick,
        || base,
        || {
            kill_calls += 1;
            Ok(GroupKill::PermissionDenied(io::Error::from_raw_os_error(1)))
        },
        || {
            exited_calls += 1;
            Ok(false)
        },
        |duration| sleeps.push(duration),
    )
    .unwrap_err();
    assert_eq!(error.raw_os_error(), Some(1));
    assert_eq!(kill_calls, 1);
    assert_eq!(exited_calls, 1);
    assert!(sleeps.is_empty());

    // Non-EPERM killpg errors and exited() errors fail closed immediately without sleeping.
    let mut exited_calls = 0;
    let mut sleeps = Vec::new();
    let error = terminate_macos_group(
        base + Duration::from_millis(10),
        tick,
        || base,
        || Err(io::Error::new(io::ErrorKind::InvalidInput, "bad pgid")),
        || {
            exited_calls += 1;
            Ok(false)
        },
        |duration| sleeps.push(duration),
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    assert_eq!(exited_calls, 0);
    assert!(sleeps.is_empty());

    let mut sleeps = Vec::new();
    let error = terminate_macos_group(
        base + Duration::from_millis(10),
        tick,
        || base,
        || Ok(GroupKill::PermissionDenied(io::Error::from_raw_os_error(1))),
        || Err(io::Error::new(io::ErrorKind::NotFound, "waitid failed")),
        |duration| sleeps.push(duration),
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::NotFound);
    assert!(sleeps.is_empty());
}

#[test]
fn traverse_indirect_git_metadata_before_executable_discovery() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("repo");
    let gitdir = tmp.path().join("worktree-meta");
    let common = tmp.path().join("common");
    let alternate = tmp.path().join("alternate");
    for p in [
        &repo,
        &gitdir,
        &common.join("objects/info"),
        &alternate.join("info"),
    ] {
        std::fs::create_dir_all(p).unwrap();
    }
    std::fs::write(repo.join(".git"), "gitdir: ../worktree-meta\n").unwrap();
    std::fs::write(gitdir.join("commondir"), "../common\n").unwrap();
    std::fs::write(common.join("objects/info/alternates"), "../../alternate\n").unwrap();
    std::fs::write(alternate.join("info/alternates"), "../common/objects\n").unwrap();
    let inner = repo.join("sub");
    std::fs::create_dir(&inner).unwrap();
    let boundaries = metadata::boundaries(&inner).unwrap();
    for expected in [&repo, &gitdir, &common, &alternate] {
        assert!(boundaries.contains(&expected.canonicalize().unwrap()));
    }
    std::fs::write(common.join("objects/info/alternates"), vec![b'x'; 32769]).unwrap();
    assert!(
        metadata::boundaries(&inner)
            .unwrap_err()
            .to_string()
            .contains("exceeds 32768")
    );
}
