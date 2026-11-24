use super::*;
use std::io::Read;

fn inputs() -> PreparedLint {
    PreparedLint {
        selected: vec!["nested/b.yml".into(), "a.yaml".into()],
        files: BTreeMap::from([
            ("a.yaml".into(), b"first\n".to_vec()),
            ("nested/b.yml".into(), b"preloaded, not on disk\n".to_vec()),
        ]),
        directories: BTreeSet::from(["support/empty".into()]),
    }
}

fn snapshot(inputs: PreparedLint) -> Result<Snapshot, String> {
    Snapshot::from_members(&inputs.selected, inputs.files, inputs.directories)
}

#[test]
fn named_snapshot_and_archive_validate_every_member() {
    let mut p = inputs();
    p.selected.push("missing.yml".into());
    assert!(snapshot(p).err().unwrap().contains("missing"));
    let mut p = inputs();
    p.selected.push("a.yaml".into());
    assert!(snapshot(p).err().unwrap().contains("duplicate"));
    for name in [
        "", "../file", "/file", "x//y", "dir/./x", "C:/x", r"a\b", "a\0b",
    ] {
        let mut p = inputs();
        p.files.insert(name.into(), vec![]);
        assert!(snapshot(p).is_err(), "{name}");
        let mut p = inputs();
        p.directories.insert(name.into());
        assert!(snapshot(p).is_err(), "{name}");
    }
    let mut p = inputs();
    p.directories.insert("a.yaml/nested".into());
    assert!(
        snapshot(p)
            .unwrap()
            .embedded_archive()
            .unwrap_err()
            .contains("collision")
    );
    let mut p = inputs();
    p.files.insert(".github/actionlint.yml".into(), vec![]);
    p.files.insert(".github/actionlint.yaml".into(), vec![]);
    assert!(snapshot(p).err().unwrap().contains("both supported"));
    let mut p = inputs();
    p.files.insert("a.yaml".into(), vec![0; 1024 * 1024 + 1]);
    assert!(snapshot(p).err().unwrap().contains("1048576-byte"));
    let mut p = inputs();
    for i in 0..255 {
        p.directories.insert(format!("dir{i}"));
    }
    assert!(snapshot(p).err().unwrap().contains("file limit"));
    let mut p = inputs();
    for i in 0..8 {
        let path = format!("wf{i}.yml");
        p.selected.push(path.clone());
        p.files.insert(path, vec![0; 1024 * 1024]);
    }
    assert!(snapshot(p).err().unwrap().contains("8388608-byte"));
}

#[test]
fn pax_records_preserve_unicode_and_explicit_directory_members() {
    let mut p = inputs();
    let long = format!("names/é{}.yaml", "x".repeat(135));
    p.files.insert(long.clone(), vec![0, 255, 1]);
    let bytes = snapshot(p).unwrap().embedded_archive().unwrap();
    assert_eq!(bytes.len(), 10240);
    let mut tar = tar::Archive::new(&bytes[..]);
    let mut names = Vec::new();
    for entry in tar.entries().unwrap() {
        let mut entry = entry.unwrap();
        let path = String::from_utf8(entry.path_bytes().into_owned()).unwrap();
        if path == long {
            let mut data = Vec::new();
            entry.read_to_end(&mut data).unwrap();
            assert_eq!(data, [0, 255, 1]);
            assert!(entry.pax_extensions().unwrap().is_some());
        }
        names.push(path);
    }
    assert!(names.contains(&long));
    assert!(names.contains(&"support/empty/".into()));
    assert!(!names.contains(&"support/".into()));
    assert!(names.contains(&".git/".into()));
}

fn trusted(executable: PathBuf) -> VettedDocker {
    let environment = vec![(
        "PATH".into(),
        executable.parent().unwrap().as_os_str().into(),
    )];
    VettedDocker {
        executable,
        environment,
    }
}

#[test]
fn embedded_client_revalidates_executable_helper_search_path_and_config_before_launch() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = absolute_path(tmp.path()).unwrap();
    let client = std::env::current_exe().unwrap();
    let client = absolute_path(&client).unwrap();
    let docker = trusted(client.clone());
    assert_eq!(docker.validate(&repo).unwrap().0, client);
    assert!(docker.validate(client.parent().unwrap()).is_err());
    let mut bad = trusted(client.clone());
    bad.environment[0].1 = std::env::join_paths([client.parent().unwrap(), &repo]).unwrap();
    assert!(
        bad.validate(&repo)
            .unwrap_err()
            .contains("helper search path")
    );
    let mut bad = trusted(client.clone());
    bad.environment.push((
        "DOCKER_CONFIG".into(),
        repo.join("missing/config").into_os_string(),
    ));
    assert!(bad.validate(&repo).unwrap_err().contains("DOCKER_CONFIG"));
    let mut bad = trusted(client);
    bad.environment.push(bad.environment[0].clone());
    assert!(bad.validate(&repo).unwrap_err().contains("duplicate key"));
    let inner = tempfile::tempdir_in(&repo).unwrap();
    let docker_runtime = Docker::from_vetted(&repo, inner.path(), &docker, &|| false).unwrap();
    assert_eq!(inner.path().read_dir().unwrap().count(), 1);
    drop(docker_runtime);
    assert_eq!(inner.path().read_dir().unwrap().count(), 0);
    std::fs::write(inner.path().join("existing"), b"fixture").unwrap();
    assert!(Docker::from_vetted(&repo, inner.path(), &docker, &|| false).is_err());
    let workspace = tempfile::tempdir().unwrap();
    let error = lint_prepared(
        &repo,
        workspace.path(),
        &docker,
        inputs(),
        Duration::ZERO,
        || false,
        |_, _| panic!("must not execute"),
    )
    .unwrap_err();
    assert_eq!(
        error.to_string(),
        "actionlint exceeded the hard 300-second total timeout"
    );
    let workspace = tempfile::tempdir().unwrap();
    let error = lint_prepared(
        &repo,
        workspace.path(),
        &docker,
        inputs(),
        Duration::from_secs(5),
        || true,
        |_, _| panic!("must not execute"),
    )
    .unwrap_err();
    assert_eq!(error, LifecycleError::Interrupted);
}

#[cfg(unix)]
#[test]
fn named_lifecycle_uses_captured_environment_and_chains_cleanup_after_a_decoder_error() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let temp = tempfile::tempdir().unwrap();
    let root = absolute_path(temp.path()).unwrap();
    let repo = root.join("checkout");
    std::fs::create_dir(&repo).unwrap();
    let exe = root.join("docker");
    std::fs::write(
        &exe,
        include_bytes!("../../../tests/fixtures/actionlint_client.sh"),
    )
    .unwrap();
    std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o700)).unwrap();
    let mut client = trusted(exe.clone());
    client
        .environment
        .push(("FIXTURE".into(), root.as_os_str().into()));
    let inside = repo.join("client");
    std::fs::write(&inside, "fixture").unwrap();
    symlink(&inside, root.join("alias")).unwrap();
    assert!(trusted(root.join("alias")).validate(&repo).is_err());
    symlink(&repo, root.join("config-alias")).unwrap();
    client.environment.push((
        "DOCKER_CONFIG".into(),
        root.join("config-alias/missing").into_os_string(),
    ));
    assert!(client.validate(&repo).is_err());
    client.environment.pop();
    let workspace = tempfile::tempdir().unwrap();
    let mut names = Vec::new();
    lint_prepared(
        &repo,
        workspace.path(),
        &client,
        inputs(),
        Duration::from_secs(10),
        || false,
        |path, json| {
            assert_eq!(json, b"[]");
            names.push(path.to_owned());
            Ok(0)
        },
    )
    .unwrap();
    assert_eq!(names, ["nested/b.yml", "a.yaml"]);
    let staged = std::fs::read(root.join("input")).unwrap();
    assert_eq!(staged, b"preloaded, not on disk\nfirst\n");
    assert!(
        !root.join("volume").exists()
            && !root.join("stage").exists()
            && !root.join("lint").exists()
    );
    let error = lint_prepared(
        &repo,
        workspace.path(),
        &client,
        inputs(),
        Duration::from_secs(10),
        || false,
        |_, _| {
            std::fs::write(root.join("invalid-volume-list"), b"").unwrap();
            Err("decoder exception".into())
        },
    )
    .unwrap_err();
    let LifecycleError::Cleanup {
        previous,
        message,
        cause,
    } = error
    else {
        panic!("{error}")
    };
    assert_eq!(
        previous.as_deref(),
        Some(&LifecycleError::Operation("decoder exception".into()))
    );
    assert_eq!(
        cause.to_string(),
        "Docker returned malformed actionlint workspace state"
    );
    assert!(
        message
            .starts_with("Docker could not remove private actionlint workspace yaga-actionlint-")
    );
    assert!(
        root.join("volume").exists(),
        "must not remove a different resource"
    );
    assert!(!root.join("stage").exists() && !root.join("lint").exists());
}

#[cfg(unix)]
#[test]
fn failed_client_launch_retains_original_os_error_across_both_cleanup_regions() {
    use std::os::unix::fs::PermissionsExt;
    let repo = tempfile::tempdir().unwrap();
    let tools = tempfile::tempdir().unwrap();
    let tools_root = absolute_path(tools.path()).unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let exe = tools_root.join("docker");
    let absent = tools_root.join("no-such-interpreter");
    std::fs::write(&exe, format!("#!{}\n", absent.display())).unwrap();
    std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o700)).unwrap();
    let error = lint_prepared(
        repo.path(),
        workspace.path(),
        &trusted(exe.clone()),
        inputs(),
        Duration::from_secs(5),
        || false,
        |_, _| panic!("unreachable"),
    )
    .unwrap_err();
    let mut prior = &error;
    for _ in 0..2 {
        let LifecycleError::Cleanup {
            previous: Some(e),
            cause,
            ..
        } = prior
        else {
            panic!("missing cleanup: {prior}")
        };
        assert!(
            matches!(&**cause, LifecycleError::ClientIo{os_code,executable:Some(path),..}
             if *os_code==libc::ENOENT && path==&exe)
        );
        prior = e;
    }
    assert!(
        matches!(prior,LifecycleError::ClientIo{message,os_code,executable:Some(path)}
       if message=="Docker control process could not be executed" && *os_code==libc::ENOENT && path==&exe)
    );
}
